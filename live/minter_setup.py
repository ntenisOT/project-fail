"""Disabled legacy minter proof.

CLOB V2 split/merge must use the collateral-adapter route. The previous direct
CTF flow is intentionally unavailable because it did not prove that minted
inventory was CLOB-settleable under V2.
"""


def main() -> None:
    print("DISABLED: port and verify CLOB V2 collateral-adapter split/merge first.")
    print("No transaction was prepared or broadcast.")


if __name__ == "__main__":
    main()
