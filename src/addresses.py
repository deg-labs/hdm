import sys


def load_addresses(file_path: str) -> dict:
    addresses = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                parts = [p.strip() for p in line.split(",")]
                address = parts[0].lower()

                if address:
                    tag = parts[1] if len(parts) > 1 and parts[1] else None
                    webhook = parts[2] if len(parts) > 2 and parts[2] else None
                    addresses[address] = {"tag": tag, "webhook": webhook}
    except IOError as e:
        sys.stderr.write(f"Error reading addresses file: {e}\n")
        sys.exit(1)

    if not addresses:
        sys.stderr.write("No addresses found in addresses file\n")
        sys.exit(1)

    return addresses
