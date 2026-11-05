# Sample export from "AutoLedger DMS" (fictional legacy system)

What a new shop hands over on day one: two CSV files exported from their old dealer management system.
The data is deliberately messy, like real exports:

- names in capitals or with extra spaces; the same customer twice with different capitalisation (C1002 / C1006)
- a customer who already exists in GarageFlow (C1001, matched by email)
- a missing email (C1004), an email without a domain suffix (C1005), an invalid phone (C1007)
- phone numbers in four formats; registration numbers with spaces and dashes
- legacy make codes (MSIL, HYUN), a two-digit year (V007), a 1975 car (V008), an unknown make code (V009)
- a vehicle whose owner is not in the export (V010)
- a registration that GarageFlow already has under another customer (V005)
- fields GarageFlow has no place for (city, loyalty points, odometer, fuel...)

Plan a migration of it against the sandbox:

    uv run flowpilot-migrate plan --source autoledger-csv --path samples/legacy/autoledger
