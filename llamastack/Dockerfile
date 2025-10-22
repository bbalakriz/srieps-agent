FROM registry.redhat.io/ubi9/python-312@sha256:e80ff3673c95b91f0dafdbe97afb261eab8244d7fd8b47e20ffcbcfee27fb168

USER root

# Install system libraries needed by OpenCV / RapidOCR
RUN yum install -y mesa-libGL  # RHEL / UBI

USER 1001

# Install Python packages at image build time
RUN pip install --upgrade pip
RUN pip install \
    docling>=2.43.0 \
    transformers \
    sentence-transformers \
    llama-stack==0.2.20 \
    llama-stack-client==0.2.20 \
    pymilvus \
    fire \
    rapidocr-onnxruntime \
    rapidocr \
    onnxruntime
