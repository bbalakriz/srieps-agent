from typing import List
import logging

from kfp import compiler, dsl
from kfp.kubernetes import add_node_selector_json, add_toleration_json

# PYTHON_BASE_IMAGE = "registry.redhat.io/ubi9/python-312@sha256:e80ff3673c95b91f0dafdbe97afb261eab8244d7fd8b47e20ffcbcfee27fb168"
PYTHON_BASE_IMAGE = "quay.io/balki404/docling-pipeline:0.0.1"
PYTORCH_CUDA_IMAGE = "quay.io/modh/odh-pipeline-runtime-pytorch-cuda-py311-ubi9@sha256:4706be608af3f33c88700ef6ef6a99e716fc95fc7d2e879502e81c0022fd840e"

_log = logging.getLogger(__name__)


# This component registers the given vector database in LlamaStack. We will use inbuilt Milvus as the vector DB provider.
@dsl.component(
    base_image=PYTHON_BASE_IMAGE,
    packages_to_install=["llama-stack-client==0.2.20", "fire", "requests"],
)
def register_vector_db(
    service_url: str,
    vector_db_id: str,
    embed_model_id: str,
):
    from llama_stack_client import LlamaStackClient

    client = LlamaStackClient(base_url=service_url)

    models = client.models.list()
    matching_model = next(
        (m for m in models if m.provider_resource_id == embed_model_id), None
    )

    if not matching_model:
        raise ValueError(
            f"Model with ID '{embed_model_id}' not found on LlamaStack server."
        )

    if matching_model.model_type != "embedding":
        raise ValueError(f"Model '{embed_model_id}' is not an embedding model")

    embedding_dimension = matching_model.metadata["embedding_dimension"]

    _ = client.vector_dbs.register(
        vector_db_id=vector_db_id,
        embedding_model=matching_model.identifier,
        embedding_dimension=embedding_dimension,
        provider_id="milvus",
    )
    print(
        f"Registered vector DB '{vector_db_id}' with embedding model '{embed_model_id}'."
    )

# This component downloads PDF files from a given base URL. We will use the PDFs from my
# personal GitHub repository which is representative of client's production knowledge base.
@dsl.component(
    base_image=PYTHON_BASE_IMAGE,
    packages_to_install=["requests"],
)
def import_test_pdfs(
    base_url: str,
    pdf_filenames: str,
    output_path: dsl.OutputPath("input-pdfs"),
):
    import os
    import requests
    import shutil

    os.makedirs(output_path, exist_ok=True)
    filenames = [f.strip() for f in pdf_filenames.split(",") if f.strip()]

    for filename in filenames:
        url = f"{base_url.rstrip('/')}/{filename.lstrip('/')}"
        file_path = os.path.join(output_path, filename)

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        try:
            with requests.get(url, stream=True, timeout=10) as response:
                response.raise_for_status()
                with open(file_path, "wb") as f:
                    shutil.copyfileobj(response.raw, f)
            print(f"Downloaded {filename}")
        except requests.exceptions.RequestException as e:
            print(f"Failed to download {filename}: {e}, skipping.")

# This component creates splits of PDF files for parallel processing
@dsl.component(
    base_image=PYTHON_BASE_IMAGE,
)
def create_pdf_splits(
    input_path: dsl.InputPath("input-pdfs"),
    num_splits: int,
) -> List[List[str]]:
    import pathlib

    print(f"Creating up to {num_splits} splits from PDFs in {input_path}")
    # Split our entire directory of pdfs into n batches, where n == num_splits
    all_pdfs = [
        str(path.relative_to(input_path))
        for path in pathlib.Path(input_path).rglob("*")
        if path.suffix.lower() == ".pdf"
    ]

    print(f"Found PDFs for processing:" , all_pdfs)

    splits = [
        batch for batch in (all_pdfs[i::num_splits] for i in range(num_splits)) if batch
    ]
    return splits or [[]]


# This component converts PDFs to Markdown and ingests the embeddings into LlamaStack's vector store
@dsl.component(
    base_image=PYTHON_BASE_IMAGE,
    packages_to_install=[
        "docling>=2.43.0",
        "transformers",
        "sentence-transformers",
        "llama-stack==0.2.20",
        "llama-stack-client==0.2.20",
        "pymilvus",
        "fire",
        "rapidocr-onnxruntime",
        "rapidocr",       
        "onnxruntime",
    ],
)
def docling_convert(
    input_path: dsl.InputPath("input-pdfs"),
    pdf_split: List[str],
    output_path: dsl.OutputPath("output-md"),
    embed_model_id: str,
    max_tokens: int,
    service_url: str,
    vector_db_id: str,
):
    import pathlib

    from docling.datamodel.base_models import InputFormat, ConversionStatus
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from transformers import AutoTokenizer
    from sentence_transformers import SentenceTransformer
    from docling.chunking import HybridChunker
    import logging
    from llama_stack_client import LlamaStackClient
    import uuid

    import json

    _log = logging.getLogger(__name__)

    # Helper functions inside the component
    def setup_chunker_and_embedder(embed_model_id: str, max_tokens: int):
        tokenizer = AutoTokenizer.from_pretrained(embed_model_id)
        embedding_model = SentenceTransformer(embed_model_id)
        chunker = HybridChunker(
            tokenizer=tokenizer, max_tokens=max_tokens, merge_peers=True
        )
        return embedding_model, chunker

    def embed_text(text: str, embedding_model) -> list[float]:
        return embedding_model.encode([text], normalize_embeddings=True).tolist()[0]

    def process_and_insert_embeddings(conv_results):
        processed_docs = 0

        for conv_res in conv_results:
            file_name = conv_res.input.file.stem

            if conv_res.status != ConversionStatus.SUCCESS:
                _log.warning(f"Conversion failed for {file_name}: {conv_res.status}")
                continue

            document = conv_res.document
            if document is None:
                _log.warning(f"Document conversion returned None for {file_name}")
                continue

            processed_docs += 1

            # Initialize embedding model and chunker per document (or could do once outside loop)
            embedding_model, chunker = setup_chunker_and_embedder(embed_model_id, max_tokens)
            embedding_dim = embedding_model.get_sentence_embedding_dimension()

            chunks_with_embedding = []

            for chunk in chunker.chunk(dl_doc=document):
                if chunk is None:
                    _log.warning(f"Skipped None chunk from document {file_name}")
                    continue

                raw_chunk = chunker.contextualize(chunk)
                if not raw_chunk or not raw_chunk.strip():
                    _log.warning(f"Skipped empty chunk from document {file_name}")
                    continue

                try:
                    embedding = embed_text(raw_chunk, embedding_model)
                except Exception as e:
                    _log.error(f"Failed to generate embedding for a chunk in {file_name}: {e}")
                    continue

                if not isinstance(embedding, list) or len(embedding) != embedding_dim:
                    _log.warning(f"Invalid embedding dimension for chunk in {file_name}")
                    continue

                chunk_id = str(uuid.uuid4())
                content_token_count = chunker.tokenizer.count_tokens(raw_chunk)

                metadata_obj = {
                    "chunk_id": chunk_id,
                    "document_id": file_name,
                    "file_name": file_name,
                    "token_count": content_token_count,
                }

                metadata_str = json.dumps(metadata_obj)
                metadata_token_count = chunker.tokenizer.count_tokens(metadata_str)
                metadata_obj["metadata_token_count"] = metadata_token_count

                chunks_with_embedding.append(
                    {
                        "chunk_metadata": metadata_obj,
                        "chunk_id": chunk_id,
                        "content": raw_chunk,
                        "mime_type": "text/markdown",
                        "embedding": embedding,
                        "metadata": metadata_obj,
                    }
                )

            # sanity check...only insert fully valid chunks
            valid_chunks = [
                c for c in chunks_with_embedding
                if c
                and isinstance(c.get("embedding"), list)
                and len(c["embedding"]) == embedding_dim
                and c.get("content") and c["content"].strip()
                and isinstance(c.get("metadata"), dict)
            ]

            if not valid_chunks:
                _log.warning(f"No valid chunks to insert for document {file_name}")
                continue

            try:
                client.vector_io.insert(vector_db_id=vector_db_id, chunks=valid_chunks)
                _log.info(f"Inserted {len(valid_chunks)} chunks for document {file_name}")
            except Exception as e:
                _log.error(f"Failed to insert chunks for document {file_name}: {e}")

        _log.info(f"Processed {processed_docs} documents successfully.")

    # Main logic starts here
    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    # Build absolute paths for the PDFs
    input_pdfs = list(input_path.rglob("*.pdf"))

    # Ensure only valid, non-empty PDFs are kept
    input_pdfs = [p for p in input_pdfs if p.exists() and p.stat().st_size > 0]

    if not input_pdfs:
        raise RuntimeError("No valid PDFs found in input_path for processing.")

    # Required models are automatically downloaded when they are
    # not provided in PdfPipelineOptions initialization
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.generate_page_images = True
    pipeline_options.ocr_options = RapidOcrOptions()

    doc_converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )

    print("PDFs for conversion:")
    for p in input_pdfs:
        print(f" - {p} (exists={p.exists()}, size={p.stat().st_size if p.exists() else 'N/A'})")

    conv_results = doc_converter.convert_all(
        input_pdfs,
        raises_on_error=True,
    )

    # Initialize LlamaStack client
    client = LlamaStackClient(base_url=service_url)

    # Process the conversion results and insert embeddings into the vector database
    process_and_insert_embeddings(conv_results)

# The main pipeline definition, making docling conversion and embedding ingestion scalable and configurable
# disabling GPU by default for broader compatibility
@dsl.pipeline()
def docling_convert_pipeline(
    base_url: str = "https://raw.githubusercontent.com/bbalakriz/rh-kcs-mcp/master",
    pdf_filenames: str = "SREIPS-Prod-troubleshooting-Knowledge-Base.pdf",
    num_workers: int = 1,
    vector_db_id: str = "sreips_vector_id",
    service_url: str = "http://lsd-llama-milvus-service:8321",
    embed_model_id: str = "ibm-granite/granite-embedding-125m-english",
    max_tokens: int = 512,
    use_gpu: bool = False,
    # tolerations: Optional[list] = [{"effect": "NoSchedule", "key": "nvidia.com/gpu", "operator": "Exists"}],
    # node_selector: Optional[dict] = {},
):
    """
    Converts PDF documents in a git repository to Markdown using Docling and generates embeddings
    :param base_url: Base URL to fetch PDF files from
    :param pdf_filenames: Comma-separated list of PDF filenames to download and convert
    :param num_workers: Number of docling worker pods to use
    :param use_gpu: Enable GPU in the docling workers
    :param vector_db_id: ID of the vector database to store embeddings
    :param service_url: URL of the Milvus service
    :param embed_model_id: Model ID for embedding generation
    :param max_tokens: Maximum number of tokens per chunk
    :return:
    """

    register_task = register_vector_db(
        service_url=service_url,
        vector_db_id=vector_db_id,
        embed_model_id=embed_model_id,
    )
    register_task.set_caching_options(False)

    import_task = import_test_pdfs(
        base_url=base_url,
        pdf_filenames=pdf_filenames,
    )
    import_task.set_caching_options(True)

    pdf_splits = create_pdf_splits(
        input_path=import_task.output,
        num_splits=num_workers,
    ).set_caching_options(True)

    with dsl.ParallelFor(pdf_splits.output) as pdf_split:
        with dsl.If(use_gpu == True):
            convert_task = docling_convert(
                input_path=import_task.output,
                pdf_split=pdf_split,
                embed_model_id=embed_model_id,
                max_tokens=max_tokens,
                service_url=service_url,
                vector_db_id=vector_db_id,
            )
            convert_task.set_caching_options(False)
            convert_task.set_cpu_request("500m")
            convert_task.set_cpu_limit("4")
            convert_task.set_memory_request("2Gi")
            convert_task.set_memory_limit("6Gi")
            convert_task.set_accelerator_type("nvidia.com/gpu")
            convert_task.set_accelerator_limit(1)
            add_toleration_json(
                convert_task,
                [
                    {
                        "effect": "NoSchedule",
                        "key": "nvidia.com/gpu",
                        "operator": "Exists",
                    }
                ],
            )
            add_node_selector_json(convert_task, {})
        with dsl.Else():
            convert_task = docling_convert(
                input_path=import_task.output,
                pdf_split=pdf_split,
                embed_model_id=embed_model_id,
                max_tokens=max_tokens,
                service_url=service_url,
                vector_db_id=vector_db_id,
            )
            convert_task.set_caching_options(False)
            convert_task.set_cpu_request("500m")
            convert_task.set_cpu_limit("4")
            convert_task.set_memory_request("2Gi")
            convert_task.set_memory_limit("6Gi")


if __name__ == "__main__":
    compiler.Compiler().compile(
        docling_convert_pipeline, package_path=__file__.replace(".py", "_compiled.yaml")
    )