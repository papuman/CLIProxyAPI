# HomeLab image: this branch (upstream release + quota-snapshot patch) + the quota-balancer plugin.
# Build from the repo root:  docker build -f homelab/Dockerfile -t local/cli-proxy-api:<tag> .
# Everything builds from reviewed source; no third-party binaries or images.
FROM golang:1.26.5-bookworm AS build
ARG PLUGIN_REPO=https://github.com/papuman/cliproxy-quota-balancer.git
ARG PLUGIN_REF=1d85102
ARG VERSION=v8.0.15-quota
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git && rm -rf /var/lib/apt/lists/*
WORKDIR /src/cpa
COPY . .
RUN go mod download && CGO_ENABLED=1 go build -buildvcs=false \
      -ldflags="-s -w -X 'main.Version=${VERSION}' -X 'main.Commit=papuman/CLIProxyAPI@homelab'" -o /out/CLIProxyAPI ./cmd/server
RUN git clone -q "$PLUGIN_REPO" /src/plugin && cd /src/plugin && git checkout -q "$PLUGIN_REF" \
 && go mod edit -replace github.com/router-for-me/CLIProxyAPI/v8=/src/cpa && go mod tidy \
 && go test ./... && CGO_ENABLED=1 go build -buildmode=c-shared -o /out/quota-balancer.so .

FROM debian:bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /CLIProxyAPI
COPY --from=build /out/CLIProxyAPI /CLIProxyAPI/CLIProxyAPI
COPY --from=build /out/quota-balancer.so /CLIProxyAPI/plugins-builtin/quota-balancer.so
EXPOSE 8317
CMD ["./CLIProxyAPI"]
