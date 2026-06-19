# Armada Bridge — IAM Validation Tests

All tests run via `isvctl test run -f isvctl/configs/providers/armada-bridge/config/iam.yaml`.

| # | Test Group | Test | What it validates |
|---|---|---|---|
| 1 | Setup | `FieldExistsCheck` (create_user) | Output from user creation contains `username` and `access_key_id` fields |
| 2 | Setup | `StepSuccessCheck` (create_user) | User and API key were created successfully |
| 3 | Credentials | `FieldExistsCheck` (test_credentials) | Output from credential test contains `account_id` field |
| 4 | Credentials | `StepSuccessCheck` (test_credentials) | Created API key authenticates successfully against Bridge |
| 5 | Teardown | `StepSuccessCheck` (teardown) | User and API key were deleted cleanly |

## Notes

- **5 validations** across 3 test groups.
- The suite exercises the full IAM user lifecycle: create → verify → delete.
- All 3 scripts (`create_user.py`, `test_credentials.py`, `delete_user.py`) are fully implemented — no stubs.
- Requires `BRIDGE_URL`, `BRIDGE_USERNAME`, `BRIDGE_PASSWORD`, `BRIDGE_TENANT` to be set.
- Optional: `BRIDGE_INSECURE=1` for self-signed TLS, `BRIDGE_TOTP_SECRET` for MFA-enabled accounts.
