from curl_cffi import requests

def test_url(url):
    print(f"Testing {url}")
    try:
        # Use impersonate to bypass TLS fingerprinting
        resp = requests.get(url, impersonate="chrome124", allow_redirects=False, timeout=10)
        print(f"Status: {resp.status_code}")
        print(f"Headers: {resp.headers.get('Location')}")
        if resp.status_code in (301, 302, 303, 307, 308):
            print(f"Redirects to: {resp.headers.get('Location')}")
        else:
            print("Response length:", len(resp.text))
            if resp.status_code == 200:
                print(resp.text[:200])
    except Exception as e:
        print("Error:", e)
    print("-" * 40)

test_url("https://jobs.micro1.ai/post/03cd460b-81ae-46c1-9453-c92e57ee2c41?referralCode=e91c9585-63ad-45aa-9820-d63708190a83&utm_source=referral&utm_medium=share&utm_campaign=job_referral")
test_url("https://click.appcast.io/t/GlqOPFOXGnpnWJXTRdC9k16_nW4uUKk3wAOhdalHnRQ=")
