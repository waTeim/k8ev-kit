# Migration Guide: All-in-One to Separated Node/Validator Architecture

This guide walks through migrating an all-in-one `eth-validator` deployment to a separated architecture using helm upgrades.

## Migration Overview

**Starting Point (Step 0):** Single deployment with Geth + Lighthouse Beacon + Validators
**End Goal (Step 4):** Two deployments - one for node infrastructure, one for validators

## Prerequisites

- Helm 3.x installed
- `kubectl` access to the cluster
- Current all-in-one deployment named `my-validator`
- Namespace: `eth` (adjust if different)

---

## Step 0: Current State (All-in-One Deployment)

**Current deployment:** `my-validator`

**Current values.yaml (deployment1-step0.yaml):**
```yaml
network: mainnet
fullnameOverride: ""

geth:
  enabled: true
  # ... other geth config

lighthouseBeacon:
  enabled: true
  checkpointSyncUrl: "https://beaconstate.ethstaker.cc/"
  # ... other beacon config

lighthouseValidator:
  enabled: true
  secretsDir:
    mountPath: "/secrets"
  # ... other validator config

externalNode:
  enabled: false
```

### Verification
```bash
# Verify all components are running
kubectl get pods -n eth -l "app.kubernetes.io/instance=my-validator"

# Expected output: 3 pods
# my-validator-geth-0
# my-validator-lighthouse-beacon-0
# my-validator-lighthouse-validator-0

# Check validators are attesting
kubectl logs -n eth my-validator-lighthouse-validator-0 --tail=50 | grep -i attest
```

---

## Step 1: Deploy Second Instance (Node-Only)

**Objective:** Create a new node deployment that validators will eventually migrate to.

### Create JWT Secret (Critical)

```bash
# Create shared JWT secret that BOTH deployments will use
python tools/create_jwt.py --name shared-jwt --namespace eth
```

**Verification:**
```bash
kubectl get secret -n eth shared-jwt
kubectl get secret -n eth shared-jwt -o jsonpath='{.data.jwt\.hex}' | base64 -d | wc -c
# Should output: 64 (32 bytes hex = 64 chars)
```

### Deploy Node Instance

**New deployment:** `my-node`

**Values file (deployment2-step1.yaml):**
```yaml
network: mainnet  # MUST match deployment 1
fullnameOverride: ""

geth:
  enabled: true
  # Copy all geth config from deployment 1
  cache: 4096
  storage:
    size: "600G"
    class: "default"
  # ... other settings from deployment 1

lighthouseBeacon:
  enabled: true
  checkpointSyncUrl: "https://beaconstate.ethstaker.cc/"
  # Copy all beacon config from deployment 1
  storage:
    size: "250G"
    class: "default"
  mev:
    enabled: true
    relays:
      # Copy relays from deployment 1
  # ... other settings from deployment 1

lighthouseValidator:
  enabled: false  # NO validators in node deployment

externalNode:
  enabled: false  # Not using external node (this IS the node)
```

**Deploy:**
```bash
helm install my-node ./eth-validator -f deployment2-step1.yaml --namespace eth
```

### Verification
```bash
# Check new pods are running
kubectl get pods -n eth -l "app.kubernetes.io/instance=my-node"

# Expected output: 2 pods
# my-node-geth-0
# my-node-lighthouse-beacon-0

# Monitor sync status (THIS IS CRITICAL - DO NOT PROCEED UNTIL SYNCED)
kubectl logs -n eth my-node-lighthouse-beacon-0 -f | grep -i sync

# Wait for "Synced" messages like:
# INFO Synced, slot: 12345678

# Check Geth sync
kubectl logs -n eth my-node-geth-0 -f | grep -i sync

# Verify endpoints are accessible
kubectl run -n eth test-curl --rm -i --tty --image=curlimages/curl -- \
  curl -s http://my-node-lighthouse-beacon:5052/eth/v1/node/health

# Should return: 200 OK (when synced)
```

**Critical:** Do NOT proceed to Step 2 until the beacon is fully synced. This can take hours to days depending on checkpoint sync.

---

## Step 2: Point Deployment 1 Validators to Deployment 2 Node

**Objective:** Migrate validators to use the new node while keeping the old node running as backup.

**CRITICAL SAFETY NOTE:** This step will cause a brief validator downtime as the pod restarts. This is intentional to prevent double-signing during the transition.

### Auto-Generate Configuration (Recommended)

Use the helper tool to automatically detect endpoints:

```bash
# Generate external config pointing to my-node deployment
python tools/generate_external_config.py my-node eth > /tmp/external-step2.yaml

# Review the generated config
cat /tmp/external-step2.yaml
```

**Example generated output:**
```yaml
externalNode:
  enabled: true
  executionEndpoint: "http://my-node-eth-validator-geth:8551"
  beaconEndpoint: "http://my-node-eth-validator-lighthouse-beacon:5052"
  jwtSecretName: "shared-jwt"

geth:
  enabled: false

lighthouseBeacon:
  enabled: false
```

### Minimum Required Values Changes (Manual Method)

**Updated values file (deployment1-step2.yaml):**
```yaml
network: mainnet  # NO CHANGE

geth:
  enabled: true  # NO CHANGE (still running old node as backup)
  # ... all other geth config unchanged

lighthouseBeacon:
  enabled: true  # NO CHANGE (still running old beacon as backup)
  # ... all other beacon config unchanged

lighthouseValidator:
  enabled: true  # NO CHANGE
  secretsDir:
    mountPath: "/secrets"  # NO CHANGE
  # ... all other validator config unchanged

# THIS IS THE ONLY SECTION THAT CHANGES:
externalNode:
  enabled: true  # CHANGED from false to true
  executionEndpoint: "http://my-node-eth-validator-geth:8551"  # NEW (use actual service name)
  beaconEndpoint: "http://my-node-eth-validator-lighthouse-beacon:5052"  # NEW (use actual service name)
  jwtSecretName: "shared-jwt"  # NEW
```

**Apply changes:**

Using auto-generated config:
```bash
# Merge with existing values
helm upgrade my-validator ./eth-validator \
  -f deployment1-step0.yaml \
  -f /tmp/external-step2.yaml \
  --namespace eth
```

Or using manual values file:
```bash
helm upgrade my-validator ./eth-validator -f deployment1-step2.yaml --namespace eth
```

### Verification
```bash
# Watch validator pod restart
kubectl get pods -n eth -l "app.kubernetes.io/name=my-validator-lighthouse-validator" -w

# Check validator is connecting to new beacon
kubectl logs -n eth my-validator-lighthouse-validator-0 --tail=100

# Should see logs referencing http://my-node-lighthouse-beacon:5052

# Verify validators are attesting on new node
kubectl logs -n eth my-validator-lighthouse-validator-0 --tail=50 | grep -i attest

# Check beacon logs on NEW node for validator connections
kubectl logs -n eth my-node-lighthouse-beacon-0 --tail=100 | grep -i validator

# Monitor for 2-3 epochs (12-18 minutes) to ensure:
# 1. No missed attestations
# 2. No slashing events
# 3. Validators are performing duties

# Check validator effectiveness
kubectl logs -n eth my-validator-lighthouse-validator-0 | grep -i "Successfully published"
```

**Expected Downtime:** 1-2 minutes during pod restart. Lighthouse doppelganger protection will cause validators to wait 2-3 epochs before attesting.

---

## Step 3: Disable Old Node in Deployment 1

**Objective:** Remove redundant Geth + Beacon from deployment 1, leaving only validators.

### Minimum Required Values Changes

**Updated values file (deployment1-step3.yaml):**
```yaml
network: mainnet  # NO CHANGE

# THESE ARE THE CHANGES:
geth:
  enabled: false  # CHANGED from true to false
  # All other geth config can remain but will be ignored

lighthouseBeacon:
  enabled: false  # CHANGED from true to false
  # All other beacon config can remain but will be ignored

lighthouseValidator:
  enabled: true  # NO CHANGE
  secretsDir:
    mountPath: "/secrets"  # NO CHANGE
  # ... all other validator config unchanged

externalNode:
  enabled: true  # NO CHANGE
  executionEndpoint: "http://my-node-geth:8551"  # NO CHANGE
  beaconEndpoint: "http://my-node-lighthouse-beacon:5052"  # NO CHANGE
  jwtSecretName: "shared-jwt"  # NO CHANGE
```

**Apply changes:**
```bash
helm upgrade my-validator ./eth-validator -f deployment1-step3.yaml --namespace eth
```

### Verification
```bash
# Check that geth and beacon pods are terminating/gone
kubectl get pods -n eth -l "app.kubernetes.io/instance=my-validator"

# Expected output: Only 1 pod
# my-validator-lighthouse-validator-0

# Verify old geth pod is gone
kubectl get pods -n eth my-validator-geth-0
# Should return: Error from server (NotFound)

# Verify old beacon pod is gone
kubectl get pods -n eth my-validator-lighthouse-beacon-0
# Should return: Error from server (NotFound)

# Verify validator is still running and attesting
kubectl logs -n eth my-validator-lighthouse-validator-0 --tail=50 | grep -i attest

# Check services were cleaned up
kubectl get svc -n eth | grep my-validator
# Should only see my-validator-lighthouse-validator (no geth or beacon services)

# Verify PVCs for old node still exist (in case rollback needed)
kubectl get pvc -n eth | grep my-validator
# Should see data volumes for geth and beacon (these can be deleted later if certain)
```

**Note:** Old Geth and Beacon PVCs are retained. Delete manually after confirming migration success for several days.

---

## Step 4: Consolidate - Delete Deployment 1 and Enable Validators in Deployment 2

**Objective:** Move validators to the node deployment and decommission the old deployment.

**CRITICAL:** This step involves deleting and recreating the validator pod. There will be a brief attestation gap.

### Step 4a: Stop Deployment 1 Validators

**CRITICAL SAFETY STEP:** Ensure validators are fully stopped before enabling in deployment 2.

```bash
# Scale down validators to zero (ensures no double-signing)
kubectl scale statefulset my-validator-lighthouse-validator --replicas=0 -n eth

# Verify validator pod is gone
kubectl get pods -n eth -l "app.kubernetes.io/name=my-validator-lighthouse-validator"
# Should return: No resources found

# Wait 30 seconds to ensure pod is fully terminated
sleep 30
```

### Step 4b: Enable Validators in Deployment 2

**CRITICAL:** Ensure the `secretsDir.mountPath` in deployment 2 points to the SAME keystore location as deployment 1.

**Updated values file (deployment2-step4.yaml):**
```yaml
network: mainnet  # NO CHANGE

geth:
  enabled: true  # NO CHANGE
  # ... all geth config unchanged

lighthouseBeacon:
  enabled: true  # NO CHANGE
  # ... all beacon config unchanged

# THIS IS THE ONLY SECTION THAT CHANGES:
lighthouseValidator:
  enabled: true  # CHANGED from false to true
  secretsDir:
    mountPath: "/secrets"  # MUST match deployment 1's path
  # Copy ALL validator config from deployment 1
  resources:
    # ... copy from deployment 1
  storage:
    # ... copy from deployment 1
  # ... all other settings

externalNode:
  enabled: false  # NO CHANGE (this deployment IS the node)
```

**Apply changes:**
```bash
helm upgrade my-node ./eth-validator -f deployment2-step4.yaml --namespace eth
```

### Verification
```bash
# Check new validator pod is running
kubectl get pods -n eth -l "app.kubernetes.io/instance=my-node"

# Expected output: 3 pods
# my-node-geth-0
# my-node-lighthouse-beacon-0
# my-node-lighthouse-validator-0  # NEW

# Check validator is starting
kubectl logs -n eth my-node-lighthouse-validator-0 --tail=100

# Verify keystores are loaded
kubectl logs -n eth my-node-lighthouse-validator-0 | grep -i "voting public key"

# Monitor attestations (wait 2-3 epochs due to doppelganger protection)
kubectl logs -n eth my-node-lighthouse-validator-0 -f | grep -i attest
```

### Step 4c: Delete Old Deployment

**Only proceed after confirming validators are healthy in deployment 2 for at least 1 hour.**

```bash
# Final verification before deletion
kubectl logs -n eth my-node-lighthouse-validator-0 --tail=50 | grep -i "Successfully published"

# Delete old deployment
helm uninstall my-validator --namespace eth

# Verify old validator pod is gone
kubectl get pods -n eth -l "app.kubernetes.io/instance=my-validator"
# Should return: No resources found

# Check remaining deployments
kubectl get pods -n eth
# Should only see my-node-* pods
```

### Step 4d: Cleanup Old PVCs (Optional)

**Wait at least 7 days before deleting old PVCs** to ensure migration is stable.

```bash
# List old PVCs
kubectl get pvc -n eth | grep my-validator

# After confirming migration is stable (7+ days), delete old PVCs
kubectl delete pvc -n eth data-my-validator-geth-0
kubectl delete pvc -n eth data-my-validator-lighthouse-beacon-0
kubectl delete pvc -n eth data-my-validator-lighthouse-validator-0
```

---

## Final Verification Checklist

After completing Step 4, verify for 24-48 hours:

- [ ] All validators are attesting every epoch
- [ ] No missed attestations beyond expected gaps during migration
- [ ] No slashing events
- [ ] Validator effectiveness > 95%
- [ ] Beacon sync is stable
- [ ] Geth sync is stable
- [ ] No error logs in any component

**Commands:**
```bash
# Check all pods healthy
kubectl get pods -n eth

# Monitor validator performance
kubectl logs -n eth my-node-lighthouse-validator-0 --tail=200 | grep -E "(attest|proposal|sync_committee)"

# Check beacon peer count
kubectl logs -n eth my-node-lighthouse-beacon-0 | grep -i "peers:"

# Check Geth peer count
kubectl logs -n eth my-node-geth-0 | grep -i "peers"

# Verify no slashing
kubectl logs -n eth my-node-lighthouse-validator-0 | grep -i slash
# Should return: nothing
```

---

## Rollback Procedures

### If Issues in Step 2 (Validators not attesting on new node)

```bash
# Rollback deployment 1 to Step 1 state
helm upgrade my-validator ./eth-validator -f deployment1-step0.yaml --namespace eth

# Validators will restart and reconnect to original node
```

### If Issues in Step 3 (Validators failing after old node removed)

```bash
# Rollback deployment 1 to Step 2 state
helm upgrade my-validator ./eth-validator -f deployment1-step2.yaml --namespace eth

# Old node will be recreated, validators remain pointing to new node
```

### If Issues in Step 4 (Validators not starting in new deployment)

```bash
# Re-enable validators in deployment 1
kubectl scale statefulset my-validator-lighthouse-validator --replicas=1 -n eth

# Disable validators in deployment 2
helm upgrade my-node ./eth-validator -f deployment2-step1.yaml --namespace eth

# Return to Step 3 state
helm upgrade my-validator ./eth-validator -f deployment1-step3.yaml --namespace eth
```

---

## Summary of Values Changes by Step

| Step | Deployment | Changed Values | Purpose |
|------|------------|----------------|---------|
| 0 | my-validator | (baseline) | All-in-one deployment |
| 1 | my-node | New deployment | Create new node |
| 2 | my-validator | `externalNode.enabled: true`<br>`externalNode.executionEndpoint`<br>`externalNode.beaconEndpoint`<br>`externalNode.jwtSecretName` | Point validators to new node |
| 3 | my-validator | `geth.enabled: false`<br>`lighthouseBeacon.enabled: false` | Remove old node |
| 4 | my-node | `lighthouseValidator.enabled: true` | Consolidate to single deployment |
| 4 | my-validator | (deleted) | Cleanup |

---

## Estimated Timeline

- **Step 0 → Step 1:** 6-24 hours (wait for new node sync)
- **Step 1 → Step 2:** 2 minutes (pod restart)
- **Step 2 → Step 3:** 1 minute (pod termination)
- **Step 3 → Step 4:** 5 minutes (validator migration)
- **Total:** 6-24 hours (mostly waiting for sync)

---

## Safety Notes

1. **Never rush the migration** - wait for full sync in Step 1
2. **Doppelganger protection** - expect 2-3 epoch delay when validators restart
3. **Monitor continuously** - watch logs during each step
4. **Keep backups** - don't delete old PVCs immediately
5. **Unique keystores** - ensure each deployment uses different keystores if running multiple validator groups
