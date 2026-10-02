# For anything that takes a container: Hugging Face Spaces, Koyeb, Fly, Cloud Run.
# The same vrp.py that runs on a laptop serves the page and the API here, so
# there is no second implementation to keep in step.
FROM python:3.12-slim

WORKDIR /app

# numpy for the core, pandas for the CSV loader, yfinance for prices.
# pytest is deliberately absent: tests run in CI, not in the image.
RUN pip install --no-cache-dir numpy pandas yfinance

COPY . .

# Hugging Face Spaces listens on 7860; every other platform overrides PORT.
ENV PORT=7860
EXPOSE 7860

CMD ["python", "vrp.py", "--no-browser"]
