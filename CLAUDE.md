# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

k8ev-kit is a modular toolkit for deploying and operating Ethereum validators on Kubernetes. It consists of three main components:

1. **eth-validator/** - Helm chart for Ethereum validators (components can be selectively enabled)
2. **lighthouse-launch/** - Go-based HTTP API server for managing Lighthouse validator operations
3. **siren/** - Helm chart for deploying Sigma Prime's Siren validator dashboard
4. **tools/** - Python utilities for secrets and password management in Kubernetes

## Development Commands

### Helm Charts (eth-validator & siren)

```bash
# Test chart installation locally
helm install <release-name> -f values/<custom-values>.yaml ./<chart-dir>

# Test with dry-run
helm install <release-name> -f values/<custom-values>.yaml ./<chart-dir> --dry-run --debug

# Check chart validity
helm lint ./<chart-dir>

# Update dependencies (if needed)
helm dependency update ./<chart-dir>

# Uninstall
helm uninstall <release-name>
```

### lighthouse-launch (Go API server)

```bash
cd lighthouse-launch

# Generate Swagger documentation (required before building)
swag init --parseDependency --parseInternal --parseDepth=1

# Build Docker image
make build
# Or with custom tag:
# IMAGE=wateim/lighthouse-launch TAG=v1.4.0 make build

# Build unstable version (uses sigp/lighthouse:latest-unstable)
make unstable

# Push to registry
make push
# Or push all versions:
# make push-all

# Clean up image
make clean
```

The API server requires Go 1.23.2+. Main dependencies:
- Echo v4 (HTTP framework)
- Swaggo (OpenAPI/Swagger generation)
- Kubernetes client-go v0.32.3

### Python tools

```bash
cd tools

# Install dependencies
pip install -r requirements.txt

# Generate password and create K8s Secret
python genpw.py -l 24 -s <secret-name> [-n <namespace>]

# Create Secret from stdin or file
echo "value" | python create_secret.py -s <secret-name> --key <key-name>
python create_secret.py -f <file> -s <secret-name> --key <key-name>

# Generate JWT token for Ethereum clients
python create_jwt.py -s <secret-name> [-n <namespace>]

# Auto-generate external node configuration
python generate_external_config.py <node-release-name> [namespace]
# Example: Point validators to "my-node" deployment
python generate_external_config.py my-node eth > external-config.yaml
helm upgrade my-validators ./eth-validator -f values.yaml -f external-config.yaml

# Set fee recipient for validators
python set_fee_recipient.py --address <eth-address>

# Add validator using keystore
python add_validator.py --keystore <path>
```

## Architecture

### Chart Structure

**eth-validator** has modular components that can be selectively enabled:
- **Geth** (execution layer) - StatefulSet with persistent storage, exposes P2P, HTTP API, and authenticated engine API
- **Lighthouse Beacon** (consensus layer) - StatefulSet with checkpoint sync support
- **Lighthouse Validator** (validator client) - StatefulSet that connects to beacon nodes
- **MEV-Boost** (optional) - Sidecar container for PBS/MEV integration
- **lighthouse-launch** - Deployment providing HTTP API for validator management

Each component can be independently enabled/disabled via `.enabled` flags.

### Deployment Flexibility

The chart can be deployed multiple times with different component combinations:

1. **Node-only deployment**: Geth + Beacon enabled, Validators disabled
2. **Validator-only deployment**: Geth + Beacon disabled, Validators enabled (uses externalNode config)
3. **All-in-one deployment**: All components enabled (traditional monolithic approach)

### Cross-Instance Configuration Pattern

When deploying validators separately from nodes, instances communicate via:

1. **JWT Secret Sharing**: Both deployments must reference the same Kubernetes Secret
   ```yaml
   # Node instance:
   geth.enabled: true
   lighthouseBeacon.enabled: true
   lighthouseValidator.enabled: false

   # Validator instance:
   externalNode:
     enabled: true
     jwtSecretName: "shared-jwt"  # Same secret as node instance
     executionEndpoint: "http://node-instance-geth:8551"
     beaconEndpoint: "http://node-instance-lighthouse-beacon:5052"
   geth.enabled: false
   lighthouseBeacon.enabled: false
   lighthouseValidator.enabled: true
   ```

2. **Service Endpoints**: Validator instances reference the node instance's ClusterIP services

3. **Network Alignment**: All instances must use the same network value (mainnet, hoodi, sepolia, etc.)

**siren** is a standalone chart for the validator dashboard that requires:
- Two secrets: API token (`apitoken` key) and session password (`password` key)
- Environment variables for beacon/validator URLs

### lighthouse-launch API

The Go server (`lighthouse-launch/main.go`) provides a REST API with Swagger documentation at `/swagger/index.html`. Key endpoints:
- Health checks and consensus readiness
- Validator keystore import/management
- Integration with Kubernetes API for watching beacon node readiness
- Automatic Lighthouse process launching after consensus is ready

The server uses:
- Echo v4 for HTTP routing
- Kubernetes client-go for in-cluster pod watching
- Structured logging (slog)
- EIP-2335 keystore format validation

### Secret Management Pattern

Python tools follow a consistent pattern:
1. Generate or read sensitive data
2. Create/update Kubernetes Secret with specific key names expected by charts
3. Use stdin piping for composability (e.g., `kubectl exec ... | create_secret.py`)

This avoids hardcoding secrets and integrates cleanly with Kubernetes-native workflows.

**JWT Secret Creation**:
```bash
# Create JWT secret for node-validator communication
python tools/create_jwt.py --name my-jwt-secret --namespace default

# This secret is used for authenticated communication between Geth and Lighthouse Beacon
# - Auto-created by eth-validator if all components are in one deployment
# - Must be manually created and shared when using separate node/validator deployments
```

## Helm Chart Publishing

Charts are automatically published to https://wateim.github.io/helm-charts when:
- A PR from `develop` → `master` is merged
- Workflow is manually triggered via `workflow_dispatch`

The workflow (`.github/workflows/publish-helm-on-merge.yml`):
1. Checks if chart versions already exist in the Helm repo index
2. Only packages/publishes new versions (version-gated)
3. Pushes to separate `waTeim/helm-charts` repository on the `main` branch

Chart versions MUST be bumped in `Chart.yaml` to trigger new releases.

## Key Configuration Patterns

### Network Selection
Set `network: mainnet` (or `holesky`, `sepolia`) in eth-validator values - this controls the `--<network>` flag for both Geth and Lighthouse.

### Storage
- Geth requires ~600GB by default (`geth.storage.size`)
- Lighthouse beacon requires ~200GB by default (`lighthouseBeacon.storage.size`)
- All use StatefulSets with PersistentVolumeClaims

### Services
Each component has dual services:
- `internal` (ClusterIP) - for API, metrics, auth endpoints
- `external` (LoadBalancer by default) - for P2P networking

External IPs can be set via `externalIp` to configure NAT traversal.

### MEV-Boost
Enabled by default in eth-validator. Configure relays via `lighthouseBeacon.mev.relays` array. The builder endpoint is automatically wired to the beacon node.

### Siren Secrets
The Siren chart requires TWO pre-existing secrets:
- `config.apiTokenSecretName` → must contain key `apitoken`
- `config.passwordSecretName` → must contain key `password`

Use `tools/genpw.py` and `tools/create_secret.py` to create these.

## Common Gotchas

1. **Secret key names**:
   - Siren requires exact key names (`apitoken`, `password`)
   - JWT secret must contain key `jwt.hex`
   - Charts won't work with different keys

2. **Beacon/Validator URLs**: Must use Kubernetes ClusterIP DNS names (e.g., `http://<release-name>-eth-validator-lighthouse-beacon:5052` where actual service names are auto-detected by `generate_external_config.py`).

3. **Chart dependencies**: Always run `helm dependency update` if charts reference subchart dependencies.

4. **JWT auth**:
   - **All-in-one deployment**: Auto-generates JWT secret
   - **Separate deployments**: Create JWT secret manually and reference it in both instances

5. **Checkpoint sync**: Highly recommended for new beacon nodes. Set `lighthouseBeacon.checkpointSyncUrl` to a trusted endpoint.

6. **External node migration**: When migrating validators between nodes:
   - Deploy new node instance and wait for full sync
   - Update validator instance's `externalNode` values to point to new node
   - Helm upgrade the validator deployment
   - Verify validators are attesting correctly before decommissioning old node

7. **Network consistency**: When using separate deployments, all instances must have matching `network` values.

8. **CRITICAL - Slashing Prevention**:
   - **NEVER deploy the same validator keystores to multiple pods**
   - Lighthouse doppelganger protection is enabled by default but takes time to activate
   - Always ensure validator keystores are unique per deployment
   - When migrating validators, fully stop the old validator pod BEFORE starting the new one
   - Use different secrets/keystore directories for different validator deployments
   - Double-check that `lighthouseValidator.secretsDir.mountPath` points to unique storage per validator instance

## Deployment Patterns

### Pattern 1: All-in-One (Simplest)
Deploy everything in a single eth-validator release:
```bash
helm install my-validator ./eth-validator -f values/all-in-one.yaml
```

Values file:
```yaml
network: mainnet
geth.enabled: true
lighthouseBeacon.enabled: true
lighthouseValidator.enabled: true
```

### Pattern 2: Separated Node and Validators (Recommended for Production)
Deploy node and validators as separate eth-validator instances:

```bash
# Step 1: Create shared JWT secret (CRITICAL: must be same for both instances)
python tools/create_jwt.py --name shared-jwt --namespace eth

# Step 2: Deploy NODE instance (no validators)
helm install my-node ./eth-validator -f values/node.yaml \
  --namespace eth

# Step 3: Wait for node sync
kubectl logs -n eth my-node-lighthouse-beacon-0 -f

# Step 4: Deploy VALIDATOR instance (no node components)
# CRITICAL: Ensure validator keystores are ONLY in this deployment, nowhere else
helm install my-validators ./eth-validator -f values/validators.yaml \
  --namespace eth
```

**values/node.yaml**:
```yaml
network: mainnet
geth:
  enabled: true
lighthouseBeacon:
  enabled: true
  checkpointSyncUrl: "https://beaconstate.ethstaker.cc/"
lighthouseValidator:
  enabled: false  # NO validators in node instance
```

**values/validators.yaml**:
```yaml
network: mainnet
externalNode:
  enabled: true
  # Use tools/generate_external_config.py to auto-detect these URLs
  executionEndpoint: "http://my-node-eth-validator-geth:8551"
  beaconEndpoint: "http://my-node-eth-validator-lighthouse-beacon:5052"
  jwtSecretName: "shared-jwt"
geth:
  enabled: false  # NO execution in validator instance
lighthouseBeacon:
  enabled: false  # NO beacon in validator instance
lighthouseValidator:
  enabled: true
  # CRITICAL: Ensure unique keystore path that is NOT used by any other deployment
  secretsDir:
    mountPath: "/secrets"  # Must point to storage containing UNIQUE keystores
```

### Pattern 3: Migration from All-in-One to Separated Architecture

**See detailed guide:** `docs/MIGRATION-GUIDE.md`

The migration guide provides step-by-step instructions for migrating from an all-in-one deployment to separated node/validator deployments using only helm upgrades.

**Quick overview:**
1. Deploy new node-only instance (geth + beacon)
2. Point existing validators to new node (via `externalNode` config)
3. Disable old node components (geth + beacon) from validator deployment
4. Consolidate validators into node deployment and delete old deployment

**CRITICAL SAFETY:** Always wait for full node sync before migrating validators, and monitor attestations carefully throughout the migration.

### Pattern 4: Multiple Validator Sets (Advanced)

Deploy multiple validator groups to different nodes:

```bash
# CRITICAL: Each validator deployment must have COMPLETELY DIFFERENT keystores

# Validator group A (pointing to node-1)
helm install validators-a ./eth-validator -f values/validators-a.yaml \
  --set externalNode.beaconEndpoint="http://node-1-lighthouse-beacon:5052"

# Validator group B (pointing to node-2)
helm install validators-b ./eth-validator -f values/validators-b.yaml \
  --set externalNode.beaconEndpoint="http://node-2-lighthouse-beacon:5052"

# VERIFY: Ensure validators-a and validators-b mount DIFFERENT keystore directories
# NEVER point two validator pods to the same keystore files
```

## Testing

Charts include basic Helm test templates in `templates/tests/test-connection.yaml` (simple curl-based connectivity tests). Run with:
```bash
helm test <release-name>
```

For lighthouse-launch, test the API locally:
```bash
cd lighthouse-launch
go run main.go
# Visit http://localhost:5000/swagger/index.html
```
