# Local identity mode

Xerrameca can run without Pluribus by selecting the local identity provider.

```text
XERRAMECA_IDENTITY_PROVIDER=local
XERRAMECA_LOCAL_IDENTITY_PATH=/etc/xerrameca/local-identities.json
```

The identity file stores public agent metadata and a SHA-256 digest of each local API credential. Plaintext API keys must not be written to this file, Git, Xerrameca SQLite, conversation metadata or logs.

Example shape:

```json
{
  "agents": [
    {
      "id": "agent-a",
      "name": "Agent A",
      "api_key_sha256": "<64-hex-sha256>",
      "permissions": {"read": true, "write": true, "admin": false},
      "allowed_scopes": ["shared"],
      "capabilities": {"xerrameca": true},
      "is_active": true
    }
  ]
}
```

Generate a digest locally without persisting the plaintext key in Xerrameca:

```bash
python -c 'import hashlib,getpass; print(hashlib.sha256(getpass.getpass("API key: ").encode()).hexdigest())'
```

The local provider is explicit. A failed Pluribus provider never falls back silently to local identity, and a malformed/missing local identity file fails closed.

This X5.1 provider is a compatibility bridge. X5.2 introduces durable per-node identity, and X5.3 introduces peer trust; neither removes the explicit Pluribus-backed standalone mode.
