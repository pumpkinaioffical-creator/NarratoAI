from playwright.sync_api import sync_playwright
import sys
import time

# Ensure we can import from project
sys.path.insert(0, '/app')

def run_verification():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = context.new_page()

        print("Navigating to Login...")
        page.goto("http://127.0.0.1:5001/login")
        
        # Check if the Promotion Popup appears automatically (due to layout inclusion and first-visit logic)
        # We expect it to appear if Promo Mode is ON and Pro System is OFF (current state)
        
        try:
            page.wait_for_selector("#pro-ad-popup", state="visible", timeout=5000)
            print("Popup appeared on Login page.")
            
            # Verify content
            content = page.content()
            if "免费额度将在次日 00:00 自动恢复" in content:
                print("Verified: Promotion Popup content matches requirements.")
                page.screenshot(path="/home/jules/verification/promo_popup_verified.png")
            else:
                print("Error: Popup appeared but content might be wrong.")
                print("Content excerpt:", content[:500]) # Log some content
                page.screenshot(path="/home/jules/verification/promo_popup_wrong_content.png")
                
        except Exception as e:
            print("Popup did not appear automatically (might have visited before in this profile?).")
            # If not appearing, we might need to reset localstorage or force it.
            # But the previous error confirmed it blocked the click, so it should appear.
            page.screenshot(path="/home/jules/verification/no_popup.png")

        browser.close()

if __name__ == "__main__":
    run_verification()
