# omnisight-contracts

The single source of truth for every payload exchanged between OmniSight components:

| Model | Direction | Endpoint / location |
| --- | --- | --- |
| `AnalyzeRequest` | client → node | `POST /v1/analyze` body |
| `AnalyzeResponse` | node → client | `200` response of `/v1/analyze` and `/api/fallback-infer` |
| `ErrorResponse` | node → client | every non-2xx response |
| `HealthResponse` | node → client | `GET /v1/health` |
| `EndpointRecord` | node → gist → clients | `omnisight-endpoint.json` in the public gist |

The package depends only on `pydantic==2.9.2`. The GPU server, the Windows client, and the CPU-only test suite can all import it.

## Install

```powershell
python -m pip install -e .          # from the repository root
```

## Validation policy

- **Requests** reject unknown fields. Base64 is decoded strictly. Images must be at most **350 KiB** after decoding, and their magic bytes must match the declared `mime`. Browser `data:` URLs are accepted and normalized.
- **Responses and records** ignore unknown fields, so older clients survive newer servers.
- `EndpointRecord.url` must be an `https://*.trycloudflare.com` origin with no path. The gist is public, so the record never carries credentials.

## JSON Schema

`shared/schema/*.schema.json` is generated from the models and committed. The web showcase derives its TypeScript types from these files.

```powershell
python -m omnisight_contracts.export_schema          # regenerate
python -m omnisight_contracts.export_schema --check  # CI drift check
```
