SHELL := /bin/bash

# Image names
DISPATCHER_IMG   := well_log_dispatcher
DEPTH_IMG        := well_log_worker_depth
CLUSTER_LITH_IMG := well_log_worker_cluster_lithology
MAPPING_LITH_IMG := well_log_worker_mapping_lithology
MERGER_IMG       := well_log_merger

# Dockerfile paths
DOCKERFILE_DISPATCHER   := build/dispatcher/Dockerfile
DOCKERFILE_DEPTH        := build/depth/Dockerfile
DOCKERFILE_CLUSTER_LITH := build/cluster_lithology/Dockerfile
DOCKERFILE_MAPPING_LITH := build/mapping_lithology/Dockerfile
DOCKERFILE_MERGER       := build/merger/Dockerfile

.PHONY: all build build-dispatcher build-depth build-cluster-lith build-mapping-lith build-merger up down clean logs status dashboard reset live batch

all: build up

## Build targets
build: build-dispatcher build-depth build-cluster-lith build-mapping-lith build-merger

build-dispatcher:
	@echo "--> Building $(DISPATCHER_IMG)..."
	docker build --network=host -t $(DISPATCHER_IMG) -f $(DOCKERFILE_DISPATCHER) ./

build-depth:
	@echo "--> Building $(DEPTH_IMG)..."
	docker build --network=host -t $(DEPTH_IMG) -f $(DOCKERFILE_DEPTH) ./

build-cluster-lith:
	@echo "--> Building $(CLUSTER_LITH_IMG)..."
	docker build --network=host -t $(CLUSTER_LITH_IMG) -f $(DOCKERFILE_CLUSTER_LITH) ./

build-mapping-lith:
	@echo "--> Building $(MAPPING_LITH_IMG)..."
	docker build --network=host -t $(MAPPING_LITH_IMG) -f $(DOCKERFILE_MAPPING_LITH) ./

build-merger:
	@echo "--> Building $(MERGER_IMG)..."
	docker build --network=host -t $(MERGER_IMG) -f $(DOCKERFILE_MERGER) ./

## Runtime targets
up:
	@echo "--> Starting full cluster..."
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