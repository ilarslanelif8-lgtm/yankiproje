import os
import json
import logging
import urllib.request
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config['SECRET_KEY'] = 'yanki-gizli-anahtar-12345'
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(BASE_DIR, "database.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

ASSISTANT_NAME = "Yankı"
MODEL_NAME = "Qwen2.5-7B (Süper Hızlı)"

SYSTEM_PROMPT = (
    "Sen 'Yankı' adında son derece akıllı, hızlı, yardımsever ve sempatik bir yapay zeka asistansın. "
    "Kullanıcının sorularına Türkçe, net, mantıklı ve seri yanıtlar ver."
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

@app.route("/")
@login_required
def index():
    return render_template("index.html", model_name=MODEL_NAME, assistant_name=ASSISTANT_NAME, user=current_user)

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        if not email or not password:
            flash("Lütfen tüm alanları doldurun.", "danger")
            return render_template("register.html")
        if User.query.filter_by(email=email).first():
            flash("Bu e-posta adresi zaten kayıtlı!", "warning")
            return render_template("register.html")
        new_user = User(email=email)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        flash("Kayıt başarılı! Şimdi giriş yapabilirsiniz.", "success")
        return redirect(url_for('login'))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash("E-posta veya şifre hatalı!", "danger")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route("/chat", methods=["POST"])
@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Geçersiz veri biçimi."}), 400

    user_message = (data.get("message") or "").strip()
    image_data = data.get("image")
    chat_history = data.get("history") or []

    if not user_message and not image_data:
        return jsonify({"error": "Mesaj boş olamaz."}), 400

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for history_item in chat_history[-6:]:
        role = history_item.get("role")
        content = history_item.get("content")
        if role in ["user", "assistant"] and content:
            messages.append({"role": role, "content": content})

    content_payload = user_message
    if image_data:
        content_payload = f"[Görsel Yüklendi] {user_message if user_message else 'Görseli açıkla.'}"

    messages.append({"role": "user", "content": content_payload})

    def generate():
        try:
            # Doğrudan açık Hugging Face Inference Router
            url = "https://router.huggingface.co/hf-inference/v1/chat/completions"
            payload = json.dumps({
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "messages": messages,
                "stream": True,
                "max_tokens": 1024,
                "temperature": 0.7
            }).encode('utf-8')

            req = urllib.request.Request(
                url, 
                data=payload, 
                headers={"Content-Type": "application/json"}
            )

            with urllib.request.urlopen(req) as response:
                for line in response:
                    line_str = line.decode('utf-8').strip()
                    if line_str.startswith("data: "):
                        data_json = line_str[6:]
                        if data_json == "[DONE]":
                            break
                        try:
                            parsed = json.loads(data_json)
                            delta = parsed["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield json.dumps({"delta": delta}, ensure_ascii=False) + "\n"
                        except Exception:
                            pass

            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"

        except Exception as e:
            logging.error(f"Yapay zeka hatası: {e}")
            yield json.dumps({"error": "Sistem şu an yanıt veremiyor, lütfen tekrar deneyin."}, ensure_ascii=False) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
