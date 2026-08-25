"""Disabled legacy minter approval command.

The V1 exchange and direct-CTF allowance sequence is invalid for the CLOB V2
adapter path. Keep this command fail-closed until the replacement is reviewed.
"""


def main() -> None:
    print("DISABLED: CLOB V2 adapter approvals are not implemented and verified.")
    print("No approval transaction was prepared or broadcast.")


if __name__ == "__main__":
    main()
