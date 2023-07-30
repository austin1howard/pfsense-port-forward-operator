# Use an official lightweight Python image.
# https://hub.docker.com/_/python
FROM python:3.11

# Poetry version...also used as version to plugin
ENV POETRY_VERSION=1.5.1
ENV POETRY_HOME=/opt/poetry

# Install poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

# Add to path
ENV PATH=/opt/poetry/bin:$PATH

# And disable poetry from making virtualenv(s)
RUN poetry config virtualenvs.create false

# Install app itself.
ENV APP_HOME /app
WORKDIR $APP_HOME

# Removes output stream buffering, allowing for more efficient logging
ENV PYTHONUNBUFFERED 1

# Install dependencies
COPY poetry.lock pyproject.toml ./
RUN poetry install --no-root --no-cache -n --without=dev

# Copy local code to the container image.
COPY oper.py .

# Run the operator on container startup
CMD kopf run --liveness=http://0.0.0.0:8080/healthz -A oper.py