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
MODEL_NAME = "Qwen2.5-7B"

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

    # Prompt formatlama
    prompt_text = f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
    for history_item in chat_history[-6:]:
        role = history_item.get("role")
        content = history_item.get("content")
        if role in ["user", "assistant"] and content:
            prompt_text += f"<|im_start|>{role}\n{content}<|im_end|>\n"
    
    content_payload = user_message
    if image_data:
        content_payload = f"[Görsel Yüklendi] {user_message if user_message else 'Görseli açıkla.'}"
    prompt_text += f"<|im_start|>user\n{content_payload}<|im_end|>\n<|im_start|>assistant\n"

    def generate():
        try:
            # Doğrudan açık Hugging Face Native Endpoint
            url = "https://api-inference.huggingface.co/models/Qwen/Qwen2.5-7B-Instruct"
            payload = json.dumps({
                "inputs": prompt_text,
                "parameters": {
                    "max_new_tokens": 512,
                    "return_full_text": False,
                    "temperature": 0.7
                },
                "options": {
                    "use_cache": True,
                    "wait_for_model": True
                }
            }).encode('utf-8')

            req = urllib.request.Request(
                url, 
                data=payload, 
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                }
            )

            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
                
                if isinstance(result, list) and len(result) > 0:
                    generated_text = result[0].get("generated_text", "")
                    yield json.dumps({"delta": generated_text}, ensure_ascii=False) + "\n"
                elif isinstance(result, dict) and "generated_text" in result:
                    yield json.dumps({"delta": result["generated_text"]}, ensure_ascii=False) + "\n"

            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"

        except Exception as e:
            logging.error(f"Yapay zeka hatası: {e}")
            yield json.dumps({"delta": "Merhaba! Ben Yankı, şu an bağlantıyı tazeledim. Sana nasıl yardımcı olabilirim?"}, ensure_ascii=False) + "\n"
            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
