# Dev acceptance environment; inference remains the pinned step-2 image.
FROM lmsysorg/sglang:v0.5.10.post1-cu130-runtime@sha256:715c461258624eae38124dcb1e1f620cbe307594fd3403ab92caf1b7017afd0f
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cmake clang g++ make ninja-build autoconf automake libtool pkg-config libssl-dev zlib1g-dev libreadline-dev \
    libmicrohttpd-dev libboost-thread-dev liblz4-dev iproute2 wireguard-tools nftables \
    iputils-ping iputils-arping tcpdump git && rm -rf /var/lib/apt/lists/*
ENV NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
ENTRYPOINT []
CMD ["sleep", "infinity"]
