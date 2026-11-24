#!/usr/bin/env python3
"""Create the portal users for a learner and print their temporary passwords.

Users map to staff records in data/adjusters.json via the username field:
  adjuster1 -> Ananya Rao (adjusters)      approver1 -> Kavita Nair, limit 2 lakh (approvers)
  approver2 -> Sunita Reddy, limit 15 lakh (approvers)   supervisor1 -> Deepa Menon (supervisors)

    python scripts/create_lab_users.py --lab-id lab --email you@example.com
"""
import secrets
import string

from kit import base_parser, outputs, session

USERS = [("adjuster1", ["adjusters"]), ("approver1", ["approvers"]),
         ("approver2", ["approvers"]), ("supervisor1", ["supervisors", "approvers"])]


def temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def main():
    p = base_parser(__doc__)
    p.add_argument("--email", required=True, help="your inbox; used as every lab user's email (plus-addressed)")
    args = p.parse_args()
    sess = session(args.region)
    out = outputs(sess, args.lab_id)
    cognito = sess.client("cognito-idp")
    local, _, domain = args.email.partition("@")
    print(f"Portal: {out['PortalUrl']}\n")
    for username, groups in USERS:
        pw = temp_password()
        email = f"{local}+{username}@{domain}"
        try:
            cognito.admin_create_user(UserPoolId=out["UserPoolId"], Username=username, TemporaryPassword=pw,
                                      MessageAction="SUPPRESS",
                                      UserAttributes=[{"Name": "email", "Value": email},
                                                      {"Name": "email_verified", "Value": "true"}])
        except cognito.exceptions.UsernameExistsException:
            cognito.admin_set_user_password(UserPoolId=out["UserPoolId"], Username=username, Password=pw, Permanent=False)
        for g in groups:
            cognito.admin_add_user_to_group(UserPoolId=out["UserPoolId"], Username=username, GroupName=g)
        print(f"  {username:<12} {', '.join(groups):<24} temporary password: {pw}")
    print("\nYou will be asked to choose a new password at first sign-in.")


if __name__ == "__main__":
    main()
