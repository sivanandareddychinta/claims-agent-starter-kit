#!/usr/bin/env python3
"""Load policies and staff into DynamoDB and upload the sample claim packs to S3.

    python scripts/seed_data.py --lab-id lab
    python scripts/seed_data.py --lab-id lab --reset-cases   # also delete every case (fresh start)
"""
import json

from kit import KIT_ROOT, PACKS_DIR, base_parser, outputs, session, to_ddb


def main():
    p = base_parser(__doc__)
    p.add_argument("--reset-cases", action="store_true", help="delete all existing cases first")
    args = p.parse_args()
    sess = session(args.region)
    out = outputs(sess, args.lab_id)
    ddb = sess.resource("dynamodb")

    for table_key, file_name in (("PoliciesTable", "policies.json"), ("AdjustersTable", "adjusters.json")):
        rows = json.loads((KIT_ROOT / "data" / file_name).read_text())
        with ddb.Table(out[table_key]).batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=to_ddb(row))
        print(f"{out[table_key]}: {len(rows)} items")

    if args.reset_cases:
        table = ddb.Table(out["CasesTable"])
        keys, kwargs = [], {"ProjectionExpression": "caseId"}
        while True:
            page = table.scan(**kwargs)
            keys += [i["caseId"] for i in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        with table.batch_writer() as batch:
            for k in keys:
                batch.delete_item(Key={"caseId": k})
        print(f"Deleted {len(keys)} cases")

    s3 = sess.client("s3")
    count = 0
    for pack_dir in sorted(PACKS_DIR.glob("PACK-*")):
        for f in pack_dir.iterdir():
            s3.upload_file(str(f), out["DocumentsBucket"], f"sample-packs/{pack_dir.name}/{f.name}")
            count += 1
    print(f"Uploaded {count} sample-pack files to s3://{out['DocumentsBucket']}/sample-packs/")


if __name__ == "__main__":
    main()
