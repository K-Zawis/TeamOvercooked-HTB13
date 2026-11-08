SHELL := /bin/bash

# Image names
DISPATCHER_IMG := well_log_dispatcher
DEPTH_IMG      := well_log_worker_depth
LITH_IMG       := well_log_worker_lithology

# Dockerfile paths
DOCKERFILE_DISPATCHER := build/dispatcher/Dockerfile
DOCKERFILE_DEPTH      := build/depth/Dockerfile
DOCKERFILE_LITH       := build/lithology/Dockerfile

.PHONY: all build build-dispatcher build-depth build-lith up down clean logs status dashboard reset live batch

all: build up logs

## Build targets
build: build-dispatcher build-depth build-lith

build-dispatcher:
	@echo "--> Building $(DISPATCHER_IMG)..."
	docker build --network=host -t $(DISPATCHER_IMG) -f $(DOCKERFILE_DISPATCHER) ./

build-depth:
	@echo "--> Building $(DEPTH_IMG)..."
	docker build --network=host -t $(DEPTH_IMG) -f $(DOCKERFILE_DEPTH) ./

build-lith:
	@echo "--> Building $(LITH_IMG)..."
	docker build --network=host -t $(LITH_IMG) -f $(DOCKERFILE_LITH) ./

## Runtime targets
up:
	@echo "--> Starting cluster..."
	docker compose up -d

down:
	@echo "--> Stopping cluster..."
	docker compose down

clean:
	@echo "--> Stopping cluster and wiping persistent volumes..."
	docker compose down -v

logs:
	docker compose logs -f

status:
	@curl -s http://localhost:8000/status

dashboard:
	@curl -s http://localhost:8000/api/dashboard

reset:
	@curl -s -X POST http://localhost:8000/reset -H "Content-Type: application/json" -d '{"force": true}'

live:
	@curl -s -X POST http://localhost:8000/live/start -H "Content-Type: application/json" -d '{}'

batch:
	@curl -s -X POST http://localhost:8000/batch/start -H "Content-Type: application/json" -d '{"batch_size": 5}'