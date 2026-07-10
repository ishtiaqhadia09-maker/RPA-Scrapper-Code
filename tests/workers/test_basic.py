from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context()

    page = context.new_page()

    page.goto("https://stagging.metutors.com/")

    print("Login manually and enter OTP...")
    input("After successful login press ENTER here...")

    context.storage_state(path="auth.json")

    print("auth.json saved successfully")

    browser.close()