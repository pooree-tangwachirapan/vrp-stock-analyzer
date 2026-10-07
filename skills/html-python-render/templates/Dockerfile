# For anything that takes a container: Hugging Face Spaces, Koyeb, Fly, Cloud Run.
# The same app.py that runs on a laptop serves the page and the API here, so
# there is no second implementation to keep in step.
FROM python:3.12-slim

WORKDIR /app

# List only what the service needs. Test runners belong in CI, not the image.
RUN pip install --no-cache-dir numpy pandas

COPY . .

# Hugging Face Spaces listens on 7860; every other platform overrides PORT.
ENV PORT=7860
EXPOSE 7860

CMD ["python", "app.py", "--no-browser"]
