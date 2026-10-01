# V11.7 native implementation, relocated for the standalone distribution.
# Build context is produced by tools/prepare_backend.py.
FROM golang:1.26.0-bookworm@sha256:2a0ba12e116687098780d3ce700f9ce3cb340783779646aafbabed748fa6677c AS source
RUN apt-get update && apt-get install -y --no-install-recommends libopus-dev protobuf-compiler && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY LocalAI.tar.gz /tmp/LocalAI.tar.gz
RUN tar -xzf /tmp/LocalAI.tar.gz -C /src
COPY localai-background-context.patch /tmp/background.patch
RUN git apply --check /tmp/background.patch && git apply /tmp/background.patch
COPY localai-diarization-staging.patch /tmp/diarization.patch
RUN git apply --check /tmp/diarization.patch && git apply /tmp/diarization.patch

FROM node:22-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS ui
WORKDIR /ui
COPY --from=source /src/core/http/react-ui/ ./
RUN npm ci --no-audit --no-fund && npm run build

FROM source AS builder
ENV GOMAXPROCS=4
ARG GOPROXY=https://proxy.golang.org,direct
ARG GOSUMDB=sum.golang.org
ENV GOPROXY=${GOPROXY} GOSUMDB=${GOSUMDB}
RUN --mount=type=cache,target=/go/pkg/mod go mod download
RUN --mount=type=cache,target=/go/pkg/mod go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.2 && go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@1958fcbe2ca8bd93af633f11e97d44e567e945af
RUN mkdir -p pkg/grpc/proto && protoc --experimental_allow_proto3_optional -Ibackend/ --go_out=pkg/grpc/proto/ --go_opt=paths=source_relative --go-grpc_out=pkg/grpc/proto/ --go-grpc_opt=paths=source_relative backend/backend.proto
COPY --from=ui /ui/dist/ /src/core/http/react-ui/dist/
COPY localai-resource-admission.patch /tmp/resource-admission.patch
RUN git apply --check /tmp/resource-admission.patch && git apply /tmp/resource-admission.patch
COPY extensions/ /src/
COPY localai-unload-tracking.patch /tmp/unload-tracking.patch
RUN git apply --check /tmp/unload-tracking.patch && git apply /tmp/unload-tracking.patch && gofmt -w pkg/model/process.go pkg/model/watchdog.go pkg/model/unload_tracking_test.go
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build go test -p 4 ./pkg/model -count=1
RUN gofmt -w core/services/nodes/resource_*.go core/services/worker/resource_*.go core/services/nodes/router.go core/services/nodes/inflight.go core/services/nodes/registry.go core/services/nodes/unloader.go core/services/nodes/model_cleanup.go core/services/worker/lifecycle.go core/services/worker/supervisor.go core/services/messaging/subjects.go
COPY localai-prepared-choices.patch /tmp/prepared-choices.patch
RUN git apply --check /tmp/prepared-choices.patch && git apply /tmp/prepared-choices.patch && gofmt -w core/http/endpoints/openai/chat.go core/http/endpoints/openai/inference.go
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build go test -p 4 ./core/http/endpoints/openai -count=1 -ginkgo.focus='ComputeChoices|streaming usage'
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build go build -p 4 -ldflags '-s -w -X github.com/mudler/LocalAI/internal.Version=v4.10.0+aitoolbox.contract3 -X github.com/mudler/LocalAI/internal.Commit=7ad0cbf259f0c7bf9920fe2438fc3630ecd6c672' -o /local-ai ./cmd/local-ai

FROM localai/localai@sha256:9f999a8cdb4d5468a6ed6574cfeab08ab1efe49466f5d88a3cc146b372c48d65 AS backends
RUN /local-ai backends install oci://quay.io/go-skynet/local-ai-backends@sha256:356e75a5da92de3d11c5ae12bb543a90cd9c2cf8631412f7525faa382e6a2dd8 cuda12-llama-cpp llama-cpp --backends-path /backends
RUN /local-ai backends install oci://quay.io/go-skynet/local-ai-backends@sha256:61d1722c3677c3a81855068092ea7dec26e3b3c3273ded39e85e19a89e229ea3 cuda13-vllm vllm --backends-path /backends
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY localai-vllm-execution.patch localai-vllm-worker-state.patch /tmp/
RUN cd /backends/cuda13-vllm && git apply --check /tmp/localai-vllm-execution.patch && git apply /tmp/localai-vllm-execution.patch && git apply --check /tmp/localai-vllm-worker-state.patch && git apply /tmp/localai-vllm-worker-state.patch
COPY python/vllm_asr_bridge.py python/vllm_execution.py /backends/cuda13-vllm/
RUN /backends/cuda13-vllm/venv/bin/python -m ensurepip --upgrade && /backends/cuda13-vllm/venv/bin/python -m pip install --no-deps soundfile==0.14.0 av==17.1.0 scipy==1.15.3 soxr==1.1.0
RUN /backends/cuda13-vllm/venv/bin/python -m py_compile /backends/cuda13-vllm/backend.py /backends/cuda13-vllm/vllm_asr_bridge.py /backends/cuda13-vllm/vllm_execution.py

# Reuse the installed, pinned native model/GPU libraries. Build only the RPC
# adapter and server glue, avoiding an unrelated CUDA/model runtime upgrade.
FROM backends AS native
RUN sed -i s,http://azure.archive.ubuntu.com/ubuntu/,https://archive.ubuntu.com/ubuntu/, /etc/apt/sources.list.d/ubuntu.sources && apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=30 -o Dir::Etc::sourceparts=- -o Dir::Etc::sourcelist=sources.list.d/ubuntu.sources update && apt-get install -y --no-install-recommends libgrpc++-dev protobuf-compiler-grpc libprotobuf-dev libabsl-dev libc-ares-dev pkg-config
RUN apt-get install -y --no-install-recommends git patch
WORKDIR /native/localai
COPY --from=source /src/backend/cpp/llama-cpp/ ./
COPY --from=source /src/backend/backend.proto ./backend.proto
COPY llama.tar.gz /tmp/llama.tar.gz
RUN mkdir -p /native/lib && cp -a /backends/cuda12-llama-cpp/lib/. /native/lib/
COPY localai-llama-preparation.patch /tmp/preparation.patch
COPY localai-llama-execution.patch /tmp/execution.patch
COPY llama_prepared_input.h ./llama_prepared_input.h
RUN mkdir -p llama.cpp && tar -xzf /tmp/llama.tar.gz --strip-components=1 -C llama.cpp && mkdir -p llama.cpp/tools/grpc-server && git apply --check /tmp/preparation.patch && git apply /tmp/preparation.patch && git apply --check /tmp/execution.patch && git apply /tmp/execution.patch && bash prepare.sh
RUN mkdir generated && protoc -I . --cpp_out=generated --grpc_out=generated --plugin=protoc-gen-grpc=/usr/bin/grpc_cpp_plugin backend.proto
RUN g++ -std=c++17 -O1 -DNDEBUG -pthread -DAITOOLBOX_ADAPTER_SHA256=\"$(sha256sum llama_prepared_input.h | cut -c1-64)\" -Igenerated -I. -Illama.cpp/tools/grpc-server -Illama.cpp/tools/server -Illama.cpp/tools/mtmd -Illama.cpp/common -Illama.cpp/include -Illama.cpp/ggml/include -Illama.cpp/src -Illama.cpp/vendor -Illama.cpp/vendor/cpp-httplib grpc-server.cpp generated/backend.pb.cc generated/backend.grpc.pb.cc -L/native/lib -Wl,-rpath-link,/native/lib -Wl,-rpath,/backends/cuda12-llama-cpp/lib -lllama-common -lllama -lmtmd -lggml -lggml-base -Wl,-Bstatic -lgrpc++_reflection $(pkg-config --libs --static grpc++ protobuf | sed 's/-lcares/-Wl,-Bdynamic -lcares -Wl,-Bstatic/g') -Wl,-Bdynamic -o /llama-cpp-contract && cp -L /usr/lib/x86_64-linux-gnu/libcares.so.2 /libcares.so.2
COPY test_llama_preparation.cpp ./test_llama_preparation.cpp
RUN g++ -std=c++17 -O1 -DNDEBUG -pthread -DAITOOLBOX_ADAPTER_SHA256=\"$(sha256sum llama_prepared_input.h | cut -c1-64)\" -Igenerated -I. -Illama.cpp/tools/grpc-server -Illama.cpp/tools/server -Illama.cpp/tools/mtmd -Illama.cpp/common -Illama.cpp/include -Illama.cpp/ggml/include -Illama.cpp/src -Illama.cpp/vendor -Illama.cpp/vendor/cpp-httplib test_llama_preparation.cpp generated/backend.pb.cc generated/backend.grpc.pb.cc -L/native/lib -Wl,-rpath-link,/native/lib -Wl,-rpath,/backends/cuda12-llama-cpp/lib -lllama-common -lllama -lmtmd -lggml -lggml-base -Wl,-Bstatic -lgrpc++_reflection $(pkg-config --libs --static grpc++ protobuf | sed 's/-lcares/-Wl,-Bdynamic -lcares -Wl,-Bstatic/g') -Wl,-Bdynamic -o /native-preparation-test && LD_LIBRARY_PATH=/native/lib /native-preparation-test

FROM backends AS runtime
COPY --from=builder /local-ai /local-ai
COPY --from=native /llama-cpp-contract /opt/aitoolbox-contract/llama-cpp-contract
COPY --from=native /libcares.so.2 /usr/lib/x86_64-linux-gnu/libcares.so.2
RUN cd /backends/cuda12-llama-cpp/lib && sha256sum libllama.so libllama-common.so libmtmd.so libggml.so libggml-base.so > /opt/aitoolbox-contract/native-libs.sha256
RUN cp /opt/aitoolbox-contract/llama-cpp-contract /backends/cuda12-llama-cpp/llama-cpp-cpu-all && cp /opt/aitoolbox-contract/llama-cpp-contract /backends/cuda12-llama-cpp/llama-cpp-grpc
LABEL org.aitoolbox.call-contract="native-preparation-v1"

LABEL org.aitoolbox.standalone="11.15.0"
