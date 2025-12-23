import re
import uuid
import os
import requests
import secrets
from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash, current_app
)
from werkzeug.security import generate_password_hash, check_password_hash
from .database import load_db, save_db
from datetime import datetime

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = load_db()
        user = db['users'].get(username)
        if user and check_password_hash(user['password_hash'], password):
            admin_status = user.get('is_admin', False)
            print(f"--- 登录诊断信息 ---")
            print(f"用户 '{username}' 正在登录。")
            print(f"从数据库读取到的 is_admin 状态是: {admin_status}")
            print(f"--------------------")
            session.permanent = True  # Make the session permanent
            session['logged_in'] = True
            session['username'] = username
            session['is_admin'] = admin_status
            session['user_avatar'] = user.get('avatar', 'default.png')
            flash('登录成功!', 'success')
            return redirect(url_for('main.index'))
        else:
            return render_template('login.html', error='无效的凭据')
    return render_template('login.html')

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = load_db()

        if username in db['users']:
            return render_template('register.html', error='用户名已存在')
        if not re.match(r'^[a-zA-Z0-9_]+$', username):
            return render_template('register.html', error='用户名只能包含字母、数字和下划线')

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
            'cerebrium_configs': []
        }

        # 自动创建用户目录
        user_pan_dir = os.path.join(current_app.instance_path, current_app.config['RESULTS_FOLDER'], username)
        os.makedirs(user_pan_dir, exist_ok=True)

        save_db(db)

        flash('注册成功! 现在可以登录了。', 'success')
        return redirect(url_for('auth.login'))
    return render_template('register.html')

@auth_bp.route('/auth/github/login')
def github_login():
    github_client_id = current_app.config['GITHUB_CLIENT_ID']
    redirect_uri = url_for('auth.github_callback', _external=True)

    # Force HTTPS if not already (for production behind proxies), unless on localhost
    if redirect_uri.startswith('http://') and '127.0.0.1' not in redirect_uri and 'localhost' not in redirect_uri:
        redirect_uri = redirect_uri.replace('http://', 'https://', 1)

    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state

    url = (
        f"https://github.com/login/oauth/authorize?"
        f"client_id={github_client_id}&"
        f"redirect_uri={redirect_uri}&"
        f"state={state}&"
        f"scope=read:user"
    )
    return redirect(url)

@auth_bp.route('/auth/github/callback')
def github_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    
    # Check if this is a binding callback vs login callback
    is_bind_mode = session.pop('github_bind_mode', False)

    if state != session.get('oauth_state'):
        flash('GitHub 授权状态验证失败，请重试', 'error')
        if is_bind_mode:
            return redirect(url_for('main.profile'))
        return redirect(url_for('auth.login'))

    github_client_id = current_app.config['GITHUB_CLIENT_ID']
    github_client_secret = current_app.config['GITHUB_CLIENT_SECRET']

    # 交换 token
    token_url = "https://github.com/login/oauth/access_token"
    headers = {'Accept': 'application/json'}
    data = {
        'client_id': github_client_id,
        'client_secret': github_client_secret,
        'code': code
    }

    response = requests.post(token_url, headers=headers, data=data)
    if response.status_code != 200:
        flash('无法获取 GitHub 访问令牌', 'error')
        if is_bind_mode:
            return redirect(url_for('main.profile'))
        return redirect(url_for('auth.login'))

    token_data = response.json()
    access_token = token_data.get('access_token')

    if not access_token:
        flash('GitHub 授权失败', 'error')
        if is_bind_mode:
            return redirect(url_for('main.profile'))
        return redirect(url_for('auth.login'))

    # 获取用户信息
    user_url = "https://api.github.com/user"
    user_headers = {
        'Authorization': f'token {access_token}',
        'Accept': 'application/json'
    }

    user_response = requests.get(user_url, headers=user_headers)
    if user_response.status_code != 200:
        flash('无法获取 GitHub 用户信息', 'error')
        if is_bind_mode:
            return redirect(url_for('main.profile'))
        return redirect(url_for('auth.login'))

    github_user = user_response.json()
    github_id = str(github_user.get('id'))
    github_login_name = github_user.get('login') # username
    github_avatar = github_user.get('avatar_url')

    db = load_db()
    
    # ======== BINDING MODE ========
    if is_bind_mode:
        if not session.get('logged_in'):
            flash('请先登录', 'error')
            return redirect(url_for('auth.login'))
        
        current_username = session.get('username')
        current_user = db['users'].get(current_username)
        
        if not current_user:
            flash('用户不存在', 'error')
            return redirect(url_for('auth.login'))

        # Check if this GitHub ID is already bound to another account
        existing_github_user = None
        existing_github_username = None
        for username, user_data in db['users'].items():
            if str(user_data.get('github_id', '')) == github_id:
                existing_github_user = user_data
                existing_github_username = username
                break

        if existing_github_user:
            if existing_github_username == current_username:
                flash('此 GitHub 账号已绑定到您的账户', 'info')
                return redirect(url_for('main.profile'))
            
            # GitHub ID exists on another account - MERGE accounts
            old_github_account = db['users'].pop(existing_github_username)
            
            # Update current user with GitHub binding info
            current_user['is_github_user'] = True
            current_user['github_id'] = github_id
            current_user['github_original_login'] = github_login_name
            if github_avatar:
                current_user['avatar'] = github_avatar
            
            # Rename current user to GitHub username
            new_username = github_login_name
            
            if new_username in db['users'] and new_username != current_username:
                counter = 1
                original = new_username
                while new_username in db['users']:
                    new_username = f"{original}_{counter}"
                    counter += 1
            
            # Move user data to new username
            db['users'].pop(current_username)
            db['users'][new_username] = current_user
            
            # Rename user folder if exists
            old_dir = os.path.join(current_app.instance_path, current_app.config['RESULTS_FOLDER'], current_username)
            new_dir = os.path.join(current_app.instance_path, current_app.config['RESULTS_FOLDER'], new_username)
            if os.path.exists(old_dir) and not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
            
            save_db(db)
            
            session['username'] = new_username
            session['user_avatar'] = current_user.get('avatar', 'default.png')
            
            flash(f'账户已合并！您的用户名已更改为 {new_username}，现在可以使用 GitHub 一键登录。', 'success')
            return redirect(url_for('main.profile'))
        
        else:
            # No existing GitHub account - simple binding
            new_username = github_login_name
            
            if new_username in db['users'] and new_username != current_username:
                counter = 1
                original = new_username
                while new_username in db['users']:
                    new_username = f"{original}_{counter}"
                    counter += 1
            
            current_user['is_github_user'] = True
            current_user['github_id'] = github_id
            current_user['github_original_login'] = github_login_name
            if github_avatar:
                current_user['avatar'] = github_avatar
            
            if new_username != current_username:
                db['users'].pop(current_username)
                db['users'][new_username] = current_user
                
                old_dir = os.path.join(current_app.instance_path, current_app.config['RESULTS_FOLDER'], current_username)
                new_dir = os.path.join(current_app.instance_path, current_app.config['RESULTS_FOLDER'], new_username)
                if os.path.exists(old_dir) and not os.path.exists(new_dir):
                    os.rename(old_dir, new_dir)
                
                session['username'] = new_username
            
            session['user_avatar'] = current_user.get('avatar', 'default.png')
            save_db(db)
            
            if new_username != current_username:
                flash(f'GitHub 绑定成功！您的用户名已更改为 {new_username}，现在可以使用 GitHub 一键登录。', 'success')
            else:
                flash('GitHub 绑定成功！现在可以使用 GitHub 一键登录。', 'success')
            
            return redirect(url_for('main.profile'))
    
    # ======== LOGIN MODE (original logic) ========
    # 1. 检查是否有用户已经绑定了这个 github_id
    target_username = None
    for username, user_data in db['users'].items():
        if str(user_data.get('github_id', '')) == github_id:
            target_username = username
            break

    # 2. 如果没有绑定，检查用户名冲突并创建/关联用户
    if not target_username:
        target_username = github_login_name

        original_username = target_username
        counter = 1
        while target_username in db['users']:
            target_username = f"{original_username}_gh_{counter}"
            counter += 1

        # 创建新用户
        api_key = str(uuid.uuid4())
        random_password = secrets.token_urlsafe(32)

        db['users'][target_username] = {
            'password_hash': generate_password_hash(random_password),
            'api_key': api_key,
            'has_invitation_code': False,
            'is_linuxdo_user': False,
            'is_github_user': True,
            'github_id': github_id,
            'github_original_login': github_login_name,
            'created_at': datetime.utcnow().isoformat(),
            'deletion_requested': False,
            'avatar': github_avatar if github_avatar else 'default.png',
            'cerebrium_configs': []
        }

        user_pan_dir = os.path.join(current_app.instance_path, current_app.config['RESULTS_FOLDER'], target_username)
        os.makedirs(user_pan_dir, exist_ok=True)

        save_db(db)
        flash(f'欢迎! 您的账户 {target_username} 已创建。', 'success')
    else:
        # 老用户登录，更新头像
        if github_avatar:
             db['users'][target_username]['avatar'] = github_avatar
             save_db(db)
        flash(f'欢迎回来, {target_username}!', 'success')

    # 执行登录
    user = db['users'][target_username]
    admin_status = user.get('is_admin', False)

    session.permanent = True
    session['logged_in'] = True
    session['username'] = target_username
    session['is_admin'] = admin_status
    session['user_avatar'] = user.get('avatar', 'default.png')

    return redirect(url_for('main.index'))

@auth_bp.route('/auth/github/bind')
def github_bind():
    """Start GitHub binding flow for logged-in users. Uses same callback as login."""
    if not session.get('logged_in'):
        flash('请先登录', 'error')
        return redirect(url_for('auth.login'))
    
    github_client_id = current_app.config['GITHUB_CLIENT_ID']
    # Use the SAME callback URL as login to avoid GitHub app configuration issues
    redirect_uri = url_for('auth.github_callback', _external=True)

    # Force HTTPS if not already (for production behind proxies)
    if redirect_uri.startswith('http://') and '127.0.0.1' not in redirect_uri and 'localhost' not in redirect_uri:
        redirect_uri = redirect_uri.replace('http://', 'https://', 1)

    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    session['github_bind_mode'] = True  # Mark that we're binding, not logging in

    url = (
        f"https://github.com/login/oauth/authorize?"
        f"client_id={github_client_id}&"
        f"redirect_uri={redirect_uri}&"
        f"state={state}&"
        f"scope=read:user"
    )
    return redirect(url)

# Note: github_bind_callback is no longer needed - binding uses github_callback with github_bind_mode flag


@auth_bp.route('/delete_account', methods=['POST'])
def delete_account():
    if not session.get('logged_in'):
        return redirect(url_for('auth.login'))

    username = session.get('username')
    db = load_db()

    if username and username in db['users']:
        db['users'][username]['deletion_requested'] = True
        save_db(db)
        flash('您的删除请求已提交，管理员将进行审核。', 'success')
        session.clear()
        return redirect(url_for('auth.login'))

    flash('无法处理您的请求。', 'error')
    return redirect(url_for('main.index'))

@auth_bp.route('/logout')
def logout():
    session.clear()
    flash('您已退出登录。', 'success')
    return redirect(url_for('auth.login'))

