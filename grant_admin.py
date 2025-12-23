import sys
import os
import uuid
from datetime import datetime
from werkzeug.security import generate_password_hash

# Ensure the project directory is in the Python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from project import create_app
from project.database import load_db, save_db

def grant_admin():
    app = create_app()
    with app.app_context():
        db = load_db()
        username = 'admin'

        if username not in db['users']:
            print(f"User '{username}' not found. Creating default admin account...")
            # Create default admin user
            password = "admin123" # Default password
            api_key = str(uuid.uuid4())

            db['users'][username] = {
                'password_hash': generate_password_hash(password),
                'api_key': api_key,
                'has_invitation_code': False,
                'is_linuxdo_user': False,
                'is_github_user': False,
                'created_at': datetime.utcnow().isoformat(),
                'deletion_requested': False,
                'avatar': 'default.png',
                'cerebrium_configs': [],
                'is_admin': True
            }

            # Create user directory
            # We need to access config which requires current_app, but we are in app_context so it's fine
            try:
                results_folder = app.config.get('RESULTS_FOLDER', 'results')
                user_pan_dir = os.path.join(app.instance_path, results_folder, username)
                os.makedirs(user_pan_dir, exist_ok=True)
            except Exception as e:
                print(f"Warning: Could not create user directory: {e}")

            save_db(db)
            print(f"Successfully created user '{username}' with password '{password}' and granted admin privileges.")
        else:
            if not db['users'][username].get('is_admin'):
                db['users'][username]['is_admin'] = True
                save_db(db)
                print(f"Successfully granted admin privileges to existing user '{username}'.")
            else:
                print(f"User '{username}' is already an admin.")

if __name__ == "__main__":
    grant_admin()
