import os
import json
import logging
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from huggingface_hub import InferenceClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config['SECRET_KEY'] = 'yanki-gizli-anahtar-12345'  # Güvenlik için gizli anahtar
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(BASE_DIR, "database.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Yapay Zeka Modeli
MODEL_ID = "Qwen/Qwen2.5-Coder-32B-Instruct"
client = InferenceClient(MODEL_ID)

ASSISTANT_NAME = "Yankı"
MODEL_NAME = "Qwen2.5-32B (Pro)"

SYSTEM_PROMPT = (
    "Sen 'Yankı' adında son derece akıllı, yardımsever ve sempatik bir yapay zeka asistansın. "
    "Kullanıcının sorularına Türkçe, mantıklı, net ve akıcı yanıtlar ver."
)

# --- VERİTABANI MODELİ ---
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

# Veritabanını otomatik oluşturma
with app.app_context():
    db.create_all()

# --- YÖNLENDİRMELER (ROUTES) ---

@app.route("/")
@login_required
def index():
    """Ana sohbet sayfası (Sadece giriş yapanlar görebilir)."""
    return render_template("index.html", model_name=MODEL_NAME, assistant_name=ASSISTANT_NAME, user=current_user)

@app.route("/register", methods=["GET", "POST"])
def register():
    """Kayıt Ol Sayfası"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
        
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Lütfen tüm alanları doldurun.", "danger")
            return render_template("register.html")

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
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
    """Giriş Yap Sayfası"""
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
    """Çıkış Yap"""
    logout_user()
    return redirect(url_for('login'))

# --- CHAT API ---

@app.route("/chat", methods=["POST"])
@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Geçersiz veri biçimi."}), 400

    user_message = (data.get("message") or "").strip()
    chat_history = data.get("history") or []

    if not user_message:
        return jsonify({"error": "Mesaj boş olamaz."}), 400

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for history_item in chat_history[-10:]:
        role = history_item.get("role")
        content = history_item.get("content")
        if role in ["user", "assistant"] and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_message})

    def generate():
        try:
            response_stream = client.chat_completion(
                messages=messages,
                max_tokens=1024,
                stream=True,
                temperature=0.7,
            )

            for chunk in response_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    token = chunk.choices[0].delta.content
                    if token:
                        yield json.dumps({"delta": token}, ensure_ascii=False) + "\n"

            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"

        except Exception as e:
            logging.error(f"Yapay zeka hatası: {e}")
            yield json.dumps({"error": "Yapay zeka yanıt üretirken bir hata oluştu."}, ensure_ascii=False) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)