FROM ubuntu:24.04

# Linux runtime for the native SpineSkeletonDataConverter (ELF x86-64).
# Binary requires glibc >= 2.38 / GLIBCXX_3.4.32 -> Ubuntu 24.04 (glibc 2.39).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/spine-converter

COPY server.py converter.py ./
COPY converter/SpineSkeletonDataConverter ./converter/

RUN chmod +x /opt/spine-converter/converter/SpineSkeletonDataConverter

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["/usr/bin/python3", "/opt/spine-converter/server.py"]