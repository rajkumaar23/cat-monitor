ARG NANO_LLM_IMAGE=dustynv/nano_llm:r36.2.0
FROM ${NANO_LLM_IMAGE}

RUN pip3 install --no-cache-dir \
      --index-url https://pypi.org/simple/ \
      'fastapi>=0.115' \
      'uvicorn[standard]' \
      pillow

COPY server.py /app/server.py
WORKDIR /app
EXPOSE 8085
CMD ["python3", "server.py"]
