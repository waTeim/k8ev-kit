#!/usr/bin/env python3
"""
Generate external node configuration for eth-validator chart.

This tool automatically detects the endpoints and configuration from an existing
node deployment and generates the values needed to connect validators to it.

Usage:
    python generate_external_config.py <node-release-name> [namespace]

Examples:
    # Generate config for node deployment named "my-node"
    python generate_external_config.py my-node

    # Generate config for node in "eth" namespace
    python generate_external_config.py my-node eth

    # Use with helm upgrade
    python generate_external_config.py my-node eth > external-config.yaml
    helm upgrade my-validators ./eth-validator -f values.yaml -f external-config.yaml
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from typing import Optional, Tuple


def run_command(cmd: list, check: bool = True) -> Tuple[bool, str]:
    """Run a shell command and return success status and output."""
    try:
        result = subprocess.run(
            cmd,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()


def check_helm_release(release_name: str, namespace: str) -> bool:
    """Check if a helm release exists."""
    success, output = run_command(
        ['helm', 'list', '-n', namespace, '-o', 'json'],
        check=False
    )
    if not success:
        return False

    try:
        releases = json.loads(output)
        return any(r['name'] == release_name for r in releases)
    except json.JSONDecodeError:
        return False


def get_helm_values(release_name: str, namespace: str) -> dict:
    """Get helm values for a release."""
    success, output = run_command(
        ['helm', 'get', 'values', release_name, '-n', namespace, '-o', 'json'],
        check=False
    )
    if not success:
        return {}

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {}


def check_secret_exists(secret_name: str, namespace: str) -> bool:
    """Check if a Kubernetes secret exists."""
    success, _ = run_command(
        ['kubectl', 'get', 'secret', secret_name, '-n', namespace],
        check=False
    )
    return success


def find_services_by_release(release_name: str, namespace: str) -> dict:
    """
    Find actual service names for a helm release by querying Kubernetes.

    Returns dict with 'geth' and 'beacon' service names (or None if not found).
    """
    # Get all services in namespace with the release label
    success, output = run_command(
        ['kubectl', 'get', 'svc', '-n', namespace,
         '-l', f'app.kubernetes.io/instance={release_name}',
         '-o', 'json'],
        check=False
    )

    if not success:
        return {'geth': None, 'beacon': None}

    try:
        data = json.loads(output)
        services = data.get('items', [])

        geth_svc = None
        beacon_svc = None

        for svc in services:
            name = svc['metadata']['name']
            svc_type = svc['spec'].get('type', '')

            # Look for internal ClusterIP services (not the -public ones)
            if svc_type == 'ClusterIP':
                # Check for geth service (contains 'geth' but not 'public')
                if 'geth' in name and 'public' not in name:
                    geth_svc = name

                # Check for lighthouse-beacon service (contains 'lighthouse-beacon' but not 'public')
                if 'lighthouse-beacon' in name and 'public' not in name:
                    beacon_svc = name

        return {'geth': geth_svc, 'beacon': beacon_svc}

    except json.JSONDecodeError:
        return {'geth': None, 'beacon': None}


def detect_jwt_secret(release_name: str, namespace: str, values: dict) -> Optional[str]:
    """Try to auto-detect the JWT secret name."""
    # Check if specified in values
    jwt_from_values = values.get('externalNode', {}).get('jwtSecretName')
    if jwt_from_values:
        return jwt_from_values

    # JWT secret always follows pattern: <release>-eth-validator-auth-jwt
    jwt_secret_name = f"{release_name}-eth-validator-auth-jwt"
    if check_secret_exists(jwt_secret_name, namespace):
        return jwt_secret_name

    return None


def generate_config(release_name: str, namespace: str = 'default') -> dict:
    """
    Generate external node configuration.

    Args:
        release_name: Name of the helm release for the node deployment
        namespace: Kubernetes namespace

    Returns:
        Dictionary with configuration and metadata
    """
    # Verify helm release exists
    if not check_helm_release(release_name, namespace):
        print(f"Error: Helm release '{release_name}' not found in namespace '{namespace}'",
              file=sys.stderr)
        sys.exit(1)

    # Get values from deployment
    values = get_helm_values(release_name, namespace)

    # Get ports or use defaults
    exec_port = values.get('geth', {}).get('internal', {}).get('auth', {}).get('port', 8551)
    beacon_port = values.get('lighthouseBeacon', {}).get('internal', {}).get('api', {}).get('port', 5052)

    # Find actual service names from Kubernetes
    print(f"Searching for services with label app.kubernetes.io/instance={release_name}...",
          file=sys.stderr)
    services = find_services_by_release(release_name, namespace)
    geth_service = services['geth']
    beacon_service = services['beacon']

    # Verify at least one service was found
    if not geth_service and not beacon_service:
        print(f"Error: Could not find Geth or Beacon services for release '{release_name}'",
              file=sys.stderr)
        print(f"Searched for services with label: app.kubernetes.io/instance={release_name}",
              file=sys.stderr)
        print("Is this a node deployment with geth and lighthouseBeacon enabled?",
              file=sys.stderr)
        sys.exit(1)

    geth_exists = geth_service is not None
    beacon_exists = beacon_service is not None

    # Detect JWT secret
    jwt_secret = detect_jwt_secret(release_name, namespace, values)

    # Build endpoints
    exec_endpoint = f"http://{geth_service}:{exec_port}" if geth_exists else ""
    beacon_endpoint = f"http://{beacon_service}:{beacon_port}" if beacon_exists else ""

    return {
        'config': {
            'externalNode': {
                'enabled': True,
                'executionEndpoint': exec_endpoint,
                'beaconEndpoint': beacon_endpoint,
                'jwtSecretName': jwt_secret or 'REPLACE_WITH_JWT_SECRET_NAME'
            },
            'geth': {
                'enabled': False
            },
            'lighthouseBeacon': {
                'enabled': False
            }
        },
        'metadata': {
            'release_name': release_name,
            'namespace': namespace,
            'geth_service': geth_service if geth_service else "(NOT FOUND)",
            'beacon_service': beacon_service if beacon_service else "(NOT FOUND)",
            'exec_endpoint': exec_endpoint,
            'beacon_endpoint': beacon_endpoint,
            'exec_port': exec_port,
            'beacon_port': beacon_port,
            'geth_exists': geth_exists,
            'beacon_exists': beacon_exists,
            'jwt_secret': jwt_secret,
            'generated_at': datetime.now().isoformat()
        }
    }


def format_yaml_config(config: dict, metadata: dict) -> str:
    """Format configuration as YAML with comments."""
    jwt_warning = ""
    if metadata['jwt_secret'] is None:
        jwt_warning = "  # WARNING: JWT secret not auto-detected - REPLACE THIS VALUE!"

    yaml_output = f"""# External node configuration for deployment: {metadata['release_name']}
# Generated: {metadata['generated_at']}
# Namespace: {metadata['namespace']}
# Detected services:
#   - Geth: {metadata['geth_service']}
#   - Beacon: {metadata['beacon_service']}

externalNode:
  enabled: true

  # Endpoints (auto-detected from running services)
  executionEndpoint: "{metadata['exec_endpoint']}"
  beaconEndpoint: "{metadata['beacon_endpoint']}"

  # JWT secret (CRITICAL: must match the node deployment)
  jwtSecretName: "{config['externalNode']['jwtSecretName']}"{jwt_warning}

# Disable local node components (validators only)
geth:
  enabled: false

lighthouseBeacon:
  enabled: false
"""
    return yaml_output


def print_summary(metadata: dict):
    """Print summary of detected configuration."""
    print("=" * 60, file=sys.stderr)
    print("External Node Configuration Generator", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Node Release: {metadata['release_name']}", file=sys.stderr)
    print(f"Namespace: {metadata['namespace']}", file=sys.stderr)
    print("", file=sys.stderr)

    print("Detected Services:", file=sys.stderr)
    if metadata['geth_exists']:
        print(f"  ✓ Geth: {metadata['geth_service']}", file=sys.stderr)
        print(f"    Endpoint: {metadata['exec_endpoint']}", file=sys.stderr)
    else:
        print(f"  ✗ Geth: NOT FOUND", file=sys.stderr)

    if metadata['beacon_exists']:
        print(f"  ✓ Beacon: {metadata['beacon_service']}", file=sys.stderr)
        print(f"    Endpoint: {metadata['beacon_endpoint']}", file=sys.stderr)
    else:
        print(f"  ✗ Beacon: NOT FOUND", file=sys.stderr)

    print("", file=sys.stderr)

    if metadata['jwt_secret']:
        print(f"  ✓ JWT Secret: {metadata['jwt_secret']}", file=sys.stderr)
    else:
        print("  ⚠ JWT Secret: NOT DETECTED - manual input required", file=sys.stderr)

    print("", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description='Generate external node configuration for eth-validator deployment',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate config for node deployment named "my-node"
  %(prog)s my-node

  # Generate config for node in "eth" namespace
  %(prog)s my-node eth

  # Save to file and use with helm
  %(prog)s my-node eth > external-config.yaml
  helm upgrade my-validators ./eth-validator -f values.yaml -f external-config.yaml
        """
    )

    parser.add_argument(
        'release_name',
        help='Name of the helm release for the node deployment'
    )
    parser.add_argument(
        'namespace',
        nargs='?',
        default='default',
        help='Kubernetes namespace (default: default)'
    )
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Suppress informational output (only output YAML)'
    )

    args = parser.parse_args()

    # Generate configuration
    result = generate_config(args.release_name, args.namespace)

    # Print summary to stderr (unless quiet)
    if not args.quiet:
        print_summary(result['metadata'])

    # Output YAML configuration to stdout
    yaml_config = format_yaml_config(result['config'], result['metadata'])
    print(yaml_config)

    # Print usage instructions to stderr (unless quiet)
    if not args.quiet:
        print("", file=sys.stderr)
        print("Usage:", file=sys.stderr)
        print(f"  # Save to file:", file=sys.stderr)
        print(f"  python {sys.argv[0]} {args.release_name} {args.namespace} > external-config.yaml",
              file=sys.stderr)
        print("", file=sys.stderr)
        print(f"  # Apply with helm upgrade:", file=sys.stderr)
        print(f"  helm upgrade my-validators ./eth-validator -f values.yaml -f external-config.yaml",
              file=sys.stderr)
        print("", file=sys.stderr)

        if result['metadata']['jwt_secret'] is None:
            print("⚠ WARNING: JWT secret name could not be determined", file=sys.stderr)
            print("Please update 'jwtSecretName' in the output before using", file=sys.stderr)
            print("", file=sys.stderr)


if __name__ == '__main__':
    main()
