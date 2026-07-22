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
app.config['SECRET_KEY'] = 'yanki-gizli-anahtar-12345'
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(BASE_DIR, "database.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

ASSISTANT_NAME = "Yankı"
MODEL_NAME = "Mistral-7B (Gerçek Yanıt)"

# Stabil çalışan ve Türkçe anlayan ücretsiz model istemcisi
client = InferenceClient(model="mistralai/Mistral-7B-Instruct-v0.2")

SYSTEM_PROMPT = (
    "Sen 'Yankı' adında akıllı, yardımsever ve sempatik bir yapay zeka asistansın. "
    "Kullanıcının sorusunu dikkatlice oku ve sadece o soruya özel Türkçe, net ve mantıklı yanıtlar ver."
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

    # Mistral formatına uygun mesaj geçmişi
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for history_item in chat_history[-4:]:
        role = history_item.get("role")
        content = history_item.get("content")
        if role in ["user", "assistant"] and content:
            messages.append({"role": role, "content": content})

    content_payload = user_message
    if image_data:
        content_payload = f"[Görsel Yüklendi] {user_message if user_message else 'Bu görseli analiz et.'}"

    messages.append({"role": "user", "content": content_payload})

    def generate():
        try:
            # Akışlı yanıt üretimi
            response = client.chat_completion(
                messages=messages,
                max_tokens=512,
                temperature=0.7,
                stream=True
            )

            for chunk in response:
                if hasattr(chunk, "choices") and chunk.choices:
                    delta = getattr(chunk.choices[0].delta, "content", "")
                    if delta:
                        yield json.dumps({"delta": delta}, ensure_ascii=False) + "\n"

            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"

        except Exception as e:
            logging.error(f"Yapay zeka hatası: {e}")
            yield json.dumps({"delta": "Üzgünüm, şu an sunucu yoğun. Lütfen sorunuzu tekrar sorun."}, ensure_ascii=False) + "\n"
            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
