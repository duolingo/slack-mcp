#!/bin/bash

# Script to run tests for slack-mcp
set -e

echo "Running Slack MCP Server tests..."

docker build -t slack-mcp-tests .
if [ -n "$CI" ]; then # check CI flag
  docker run -t \
    --env CI="$CI" \
    slack-mcp-tests sh -c "uv sync --extra test && uv run pytest tests/ -v"
else
  docker run -t \
    slack-mcp-tests sh -c "uv sync --extra test && uv run pytest tests/ -v"
fi

echo "✅ All tests passed!"
