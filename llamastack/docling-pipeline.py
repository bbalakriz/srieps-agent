from typing import List
import logging

from kfp import compiler, dsl
from kfp.kubernetes import add_node_selector_json, add_toleration_json

# PYTHON_BASE_IMAGE = "registry.redhat.io/ubi9/python-312@sha256:e80ff3673c95b91f0dafdbe97afb261eab8244d7fd8b47e20ffcbcfee27fb168"
# bake deps in Containerfile; avoid packages_to_install (pip as uid 1001 fails on site-packages)
PYTHON_BASE_IMAGE = "quay.io/balki404/docling-pipeline:0.0.6"
PYTORCH_CUDA_IMAGE = "quay.io/modh/odh-pipeline-runtime-pytorch-cuda-py311-ubi9@sha256:4706be608af3f33c88700ef6ef6a99e716fc95fc7d2e879502e81c0022fd840e"

_log = logging.getLogger(__name__)


# This component registers the given vector database in LlamaStack. We will use inbuilt Milvus as the vector DB provider.
@dsl.component(base_image=PYTHON_BASE_IMAGE)
def register_vector_db(
    service_url: str,
    vector_db_id: str,
    embed_model_id: str,
) -> str:
    from llama_stack_client import LlamaStackClient

    client = LlamaStackClient(base_url=service_url)

    available = []
    stack_model_id = None
    embedding_dimension = 768
    for m in client.models.list():
        md = m.custom_metadata or {}
        if md.get("model_type") != "embedding":
            continue
        provider_id = md.get("provider_resource_id", "")
        available.append(provider_id)
        if provider_id == embed_model_id or m.id.endswith("/" + embed_model_id):
            stack_model_id = m.id
            embedding_dimension = int(md.get("embedding_dimension", 768))
            break
    if not stack_model_id:
        raise ValueError(
            f"Model with ID '{embed_model_id}' not found on LlamaStack server. "
            f"Available embedding models: {available}"
        )

    vector_store = client.vector_stores.create(
        name=vector_db_id,
        extra_body={
            "provider_id": "milvus-remote",
            "embedding_model": stack_model_id,
            "embedding_dimension": embedding_dimension,
        },
    )
    print(
        f"Created vector store '{vector_db_id}' with embedding model '{stack_model_id}' "
        f"(vector store ID '{vector_store.id}')."
    )

    return vector_store.id

# This component downloads PDF files from a given base URL. We will use the PDFs from my
# personal GitHub repository which is representative of client's production knowledge base.
@dsl.component(base_image=PYTHON_BASE_IMAGE)
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
@dsl.component(base_image=PYTHON_BASE_IMAGE)
def docling_convert(
    input_path: dsl.InputPath("input-pdfs"),
    pdf_split: List[str],
    output_path: dsl.OutputPath("output-md"),
    embed_model_id: str,
    max_tokens: int,
    service_url: str,
    vector_store_id: str,
):
    import os
    import pathlib
    import hashlib
    import json
    import logging
    from typing import List

    # openshift runs as uid 1001; default HF cache under $HOME is not writable
    hf_cache = os.environ.get(
        "HF_HOME", "/opt/app-root/model-cache/huggingface"
    )
    st_cache = os.environ.get(
        "SENTENCE_TRANSFORMERS_HOME",
        "/opt/app-root/model-cache/sentence-transformers",
    )
    for path in (hf_cache, st_cache):
        os.makedirs(path, exist_ok=True)
    os.environ["HF_HOME"] = hf_cache
    os.environ["TRANSFORMERS_CACHE"] = hf_cache
    os.environ["HUGGINGFACE_HUB_CACHE"] = hf_cache
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = st_cache

    from docling.datamodel.base_models import InputFormat, ConversionStatus
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from transformers import AutoTokenizer
    from sentence_transformers import SentenceTransformer
    from docling.chunking import HybridChunker
    from llama_stack_client import LlamaStackClient

    def setup_rapidocr_options() -> RapidOcrOptions:
        rapidocr_dir = os.environ.get(
            "RAPIDOCR_MODEL_DIR", "/opt/app-root/model-cache/rapidocr"
        )
        os.makedirs(rapidocr_dir, exist_ok=True)
        root = pathlib.Path(rapidocr_dir)

        def first_match(*patterns: str) -> str | None:
            for pattern in patterns:
                matches = sorted(root.rglob(pattern))
                if matches:
                    return str(matches[0])
            return None

        det = first_match("ch_PP-OCRv4_det_mobile.onnx", "*det*.onnx")
        cls = first_match("ch_ppocr_mobile_v2.0_cls_infer.onnx", "*cls*.onnx")
        rec = first_match("ch_PP-OCRv4_rec_mobile_infer.onnx", "*rec*.onnx")
        keys = first_match("*dict*.txt", "*keys*.txt")

        kwargs: dict = {}
        if det:
            kwargs["det_model_path"] = det
        if cls:
            kwargs["cls_model_path"] = cls
        if rec:
            kwargs["rec_model_path"] = rec
        if keys:
            kwargs["rec_keys_path"] = keys
        if not kwargs:
            kwargs["rapidocr_params"] = {"Global.model_root_dir": rapidocr_dir}
        return RapidOcrOptions(**kwargs)

    _log = logging.getLogger(__name__)
    
    def setup_chunker_and_embedder(embed_model_id: str, max_tokens: int):  
        """Initialize the custom chunker and embedding model"""  
        tokenizer = AutoTokenizer.from_pretrained(embed_model_id)  
        embedding_model = SentenceTransformer(embed_model_id)  
        chunker = HybridChunker(  
            tokenizer=tokenizer, max_tokens=max_tokens, merge_peers=True  
        )  
        return embedding_model, chunker  
    
    def embed_text(text: str, embedding_model) -> List[float]:  
        """Generate embedding for text using the custom model"""  
        return embedding_model.encode([text], normalize_embeddings=True).tolist()[0]  
    
    def resolve_stack_embedding_model(client, provider_embed_id: str) -> tuple[str, int]:
        for m in client.models.list():
            md = m.custom_metadata or {}
            if md.get("model_type") != "embedding":
                continue
            if md.get("provider_resource_id") == provider_embed_id or m.id.endswith(
                "/" + provider_embed_id
            ):
                return m.id, int(md.get("embedding_dimension", 768))
        raise ValueError(f"Embedding model '{provider_embed_id}' not found on LlamaStack")

    def process_and_insert_with_custom_chunking(
        conv_results,
        vector_store_id: str,
        embed_model_id: str,
        max_tokens: int = 512,
        batch_size: int = 32,
    ):
        """Process documents using custom chunker and precomputed embeddings."""
        processed_docs = 0
        client = LlamaStackClient(base_url=service_url)
        stack_model_id, embedding_dim = resolve_stack_embedding_model(client, embed_model_id)
        embedding_model, chunker = setup_chunker_and_embedder(embed_model_id, max_tokens)

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
            chunks = []

            for chunk in chunker.chunk(dl_doc=document):
                if chunk is None:
                    continue

                raw_chunk = chunker.contextualize(chunk)
                if not raw_chunk or not raw_chunk.strip():
                    continue

                chunk_id = hashlib.sha256(f"{file_name}:{raw_chunk}".encode()).hexdigest()
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "content": raw_chunk,
                        "chunk_metadata": {
                            "document_id": file_name,
                            "file_name": file_name,
                            "chunk_id": chunk_id,
                            "source": "docling_hybrid_chunker",
                        },
                        "embedding": embed_text(raw_chunk, embedding_model),
                        "embedding_model": stack_model_id,
                        "embedding_dimension": embedding_dim,
                    }
                )

            if not chunks:
                continue

            try:
                for i in range(0, len(chunks), batch_size):
                    client.vector_io.insert(
                        vector_store_id=vector_store_id,
                        chunks=chunks[i : i + batch_size],
                    )
                _log.info(f"Inserted {len(chunks)} chunks for {file_name}")
            except Exception as e:
                _log.error(f"Failed to insert chunks for {file_name}: {e}")

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
    pipeline_options.ocr_options = setup_rapidocr_options()

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

    # Process the conversion results and insert embeddings into the vector database
    process_and_insert_with_custom_chunking( conv_results=conv_results,  
        vector_store_id=vector_store_id,  
        embed_model_id=embed_model_id,  
        max_tokens=512)

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
                vector_store_id=register_task.output,
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
                vector_store_id=register_task.output,
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