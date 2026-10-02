FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv

COPY app ./app
COPY tests ./tests
COPY verify.sh ./verify.sh
RUN chmod +x verify.sh

EXPOSE 8080
CMD ["python3", "-m", "app.server"]
