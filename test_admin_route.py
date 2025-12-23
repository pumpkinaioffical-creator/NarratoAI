"""Test admin route to diagnose 500 error"""
import sys
import traceback

try:
    from project import create_app
    from project.database import load_db
    
    app = create_app()
    app.config['TESTING'] = True
    app.config['DEBUG'] = True
    
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['username'] = 'admin'
            sess['is_admin'] = True
        
        print("Testing /admin/ route...")
        resp = client.get('/admin/')
        print(f"Status: {resp.status_code}")
        
        if resp.status_code == 500:
            print("\n=== ERROR RESPONSE ===")
            print(resp.data.decode()[:3000])
        elif resp.status_code == 200:
            print("SUCCESS: Admin panel loaded correctly")
        else:
            print(f"Response: {resp.data.decode()[:500]}")
            
except Exception as e:
    print(f"Exception: {e}")
    traceback.print_exc()
