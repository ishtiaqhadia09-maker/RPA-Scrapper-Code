from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context(
        storage_state="auth/metutors_auth.json"
    )

    page = context.new_page()

    page.goto("https://stagging.metutors.com/student/dashboard")

    print("Already logged in")

    input("Press ENTER to close browser")

    browser.close()