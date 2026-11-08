SHELL := /bin/bash

# Image names
DISPATCHER_IMG := well_log_dispatcher
WORKER_DEPTH_IMG := well_log_worker_depth
WORKER_LITH_IMG := well_log_worker_lithology
MERGER_IMG := well_log_merger

# Dockerfile paths
DOCKERFILE_DIR := build
DOCKERFILE_DISPATCHER := $(DOCKERFILE_DIR)/Dockerfile.dispatcher
DOCKERFILE_DEPTH := $(DOCKERFILE_DIR)/Dockerfile.worker_depth
DOCKERFILE_LITH := $(DOCKERFILE_DIR)/Dockerfile.worker_lithology
DOCKERFILE_MERGER := $(DOCKERFILE_DIR)/Dockerfile.merger

.PHONY: all build build-dispatcher build-depth build-lith build-merger up down clean logs status dashboard reset reconcile live batch help

# Default target
all: build up

## Build targets
build: build-dispatcher build-depth build-lith build-merger

build-dispatcher:
	@echo "--> Building $(DISPATCHER_IMG)..."
	docker build --network=host -t $(DISPATCHER_IMG) -f $(DOCKERFILE_DISPATCHER) ./

build-depth:
	@echo "--> Building $(WORKER_DEPTH_IMG)..."
	docker build --network=host -t $(WORKER_DEPTH_IMG) -f $(DOCKERFILE_DEPTH) ./

build-lith:
	@echo "--> Building $(WORKER_LITH_IMG)..."
	docker build --network=host -t $(WORKER_LITH_IMG) -f $(DOCKERFILE_LITH) ./

build-merger:
	@echo "--> Building $(MERGER_IMG)..."
	docker build --network=host -t $(MERGER_IMG) -f $(DOCKERFILE_MERGER) ./

## Runtime targets
up:
	@echo "--> Launching cluster..."
	docker compose up -d

down:
	@echo "--> Stopping cluster..."
	docker compose down

clean:
	@echo "--> Stopping cluster and wiping Redis persistent volumes..."
	docker compose down -v

logs:
	docker compose logs -f

logs-dispatcher:
	docker logs -f well_log_dispatcher

## Pipeline control helpers
status:
	@curl -s http://localhost:8000/status | jq . 2>/dev/null || curl -s http://localhost:8000/status

dashboard:
	@curl -s http://localhost:8000/api/dashboard | jq . 2>/dev/null || curl -s http://localhost:8000/api/dashboard

reconcile:
	@curl -s -X POST http://localhost:8000/batch/reconcile | jq . 2>/dev/null || curl -s -X POST http://localhost:8000/batch/reconcile

reset:
	@curl -s -X POST http://localhost:8000/reset -H "Content-Type: application/json" -d '{"force": true}' | jq . 2>/dev/null || curl -s -X POST http://localhost:8000/reset -H "Content-Type: application/json" -d '{"force": true}'

live:
	@curl -s -X POST http://localhost:8000/live/start -H "Content-Type: application/json" -d '{}' | jq . 2>/dev/null || curl -s -X POST http://localhost:8000/live/start -H "Content-Type: application/json" -d '{}'

batch:
	@curl -s -X POST http://localhost:8000/batch/start -H "Content-Type: application/json" -d '{"batch_size": 5}' | jq . 2>/dev/null || curl -s -X POST http://localhost:8000/batch/start -H "Content-Type: application/json" -d '{"batch_size": 5}'

help:
	@echo "Available commands:"
	@echo "  make build             - Build all 4 container images"
	@echo "  make up                - Start containers via docker compose"
	@echo "  make down              - Stop all containers"
	@echo "  make clean             - Stop containers and remove Redis volumes"
	@echo "  make logs              - Stream logs for all services"
	@echo "  make status            - Inspect dispatcher orchestrator state"
	@echo "  make dashboard         - Query queues and per-document progress"
	@echo "  make live              - Trigger single-log live demo run"
	@echo "  make batch             - Trigger 5-log chunked batch run"
	@echo "  make reconcile         - Audit active streams and self-heal stuck barriers"
	@echo "  make reset             - Flush Redis database and reset orchestrator"