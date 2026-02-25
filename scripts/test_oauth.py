import json
from x.xCustom import CustomActionHandler

def main():
    h = CustomActionHandler(timeout=10)
    try:
        out = h.xOAuthFormRequest('https://httpbin.org/post;myclient;mysecret;client_credentials;scope1 scope2;')
        print('RESULT_OK')
        try:
            # Pretty print JSON response
            parsed = json.loads(out)
            print(json.dumps(parsed, indent=2))
        except Exception:
            print(out)
    except Exception as e:
        print('ERROR')
        print(str(e))

if __name__ == '__main__':
    main()


