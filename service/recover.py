"""Server operator receipt recovery, never a public API or a publication command."""
import argparse
import json
import os
import sys
from service import configured_service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload-file', required=True, help='Exact original approved submission JSON; keep private')
    parser.add_argument('--confirmed-issue-number', required=True, type=int,
                        help='Owner-confirmed association, established independently of text matching')
    args = parser.parse_args()
    try:
        if os.environ.get('FEEDBACK_ENABLED') != '0': raise ValueError('publication must be disabled')
        if not os.path.isfile(os.environ['FEEDBACK_DATABASE']): raise ValueError('existing database required')
        with open(args.payload_file, 'rb') as source:
            data = source.read(24001)
        if len(data) > 24000: raise ValueError('invalid size')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('duplicate key')
                result[key] = value
            return result
        payload = json.loads(data, object_pairs_hook=unique)
        configured_service().recover(payload, args.confirmed_issue_number)
    except Exception:
        # Never print exception text, upstream bodies, payloads, credentials or file names.
        print('Recovery refused or unconfirmed. Keep the attempt; do not publish again.', file=sys.stderr)
        return 1
    print('Receipt confirmed and durably recorded. The client may check its outcome.')
    return 0


if __name__ == '__main__': sys.exit(main())
