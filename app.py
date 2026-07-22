import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from duckduckgo_search import DDGS

logging.basicConfig(level=logging.INFO)

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
MODEL_NAME = "Yankı-AI"

SYSTEM_PROMPT = (
    "Sen 'Yankı' adında akıllı, samimi ve yardımsever bir yapay zeka asistansın. "
    "Kullanıcıya Türkçe, düzgün, imla kurallarına uygun, nazik ve net yanıtlar ver. "
    "Sana sunulan internet arama sonuçlarını kullanarak güncel bilgileri (döviz, altın, hava durumu, saat, haberler) doğru şekilde sun. "
    "Eğer kullanıcı görsel gönderdiyse görseldeki detayları inceleyip doğru cevap ver."
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    messages = db.relationship('ChatMessage', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False) # 'user' veya 'assistant'
    content = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

def search_internet(query):
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, region='tr-tr', max_results=4):
                results.append(f"Başlık: {r.get('title')}\nÖzet: {r.get('body')}")
        return "\n\n".join(results)
    except Exception as e:
        logging.error(f"Arama hatası: {e}")
        return ""

@app.route("/")
@login_required
def index():
    # Kullanıcının geçmiş mesajlarını veritabanından getir
    history_messages = ChatMessage.query.filter_by(user_id=current_user.id).order_by(ChatMessage.timestamp.asc()).all()
    return render_template("index.html", model_name=MODEL_NAME, assistant_name=ASSISTANT_NAME, user=current_user, history=history_messages)

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
        login_user(new_user, remember=True) # Oturumu sürekli açık tut
        return redirect(url_for('index'))
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
            login_user(user, remember=True) # Oturumu kapatana kadar hatırla
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
@login_required
def chat():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Geçersiz veri."}), 400

    user_message = (data.get("message") or "").strip()
    image_base64 = data.get("image")

    if not user_message and not image_base64:
        return jsonify({"error": "Mesaj veya görsel boş olamaz."}), 400

    # Kullanıcı mesajını veritabanına kaydet
    user_msg_record = ChatMessage(user_id=current_user.id, role="user", content=user_message, image_url=image_base64)
    db.session.add(user_msg_record)
    db.session.commit()

    # Veritabanından son 10 mesajı alıp hafızaya yükle
    recent_db_messages = ChatMessage.query.filter_by(user_id=current_user.id).order_by(ChatMessage.timestamp.desc()).limit(10).all()
    recent_db_messages.reverse()

    api_key = os.environ.get("GROQ_API_KEY", "")

    search_context = ""
    if user_message and not image_base64:
        search_data = search_internet(user_message)
        if search_data:
            search_context = f"\n\n[İnternet Canlı Bilgileri]:\n{search_data}\n\nBu canlı internet bilgilerini kullanarak yanıt ver."

    messages = [{"role": "system", "content": SYSTEM_PROMPT + search_context}]
    for m in recent_db_messages[:-1]:
        if m.content:
            messages.append({"role": m.role, "content": m.content})

    if image_base64:
        model_name = "llama-3.2-90b-vision-preview"
        content_payload = [
            {"type": "text", "text": user_message if user_message else "Bu görselde ne var? Detaylıca açıkla."},
            {"type": "image_url", "image_url": {"url": image_base64}}
        ]
        messages.append({"role": "user", "content": content_payload})
    else:
        model_name = "llama-3.3-70b-versatile"
        messages.append({"role": "user", "content": user_message})

    def generate():
        if not api_key:
            yield json.dumps({"delta": "Sistem API anahtarı eksik."}, ensure_ascii=False) + "\n"
            yield json.dumps({"done": True}) + "\n"
            return

        full_assistant_reply = ""
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": model_name,
                "messages": messages,
                "stream": True
            }

            res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, stream=True, timeout=30)

            if res.status_code == 200:
                for line in res.iter_lines():
                    if line:
                        line_str = line.decode('utf-8')
                        if line_str.startswith("data: "):
                            data_str = line_str[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                parsed = json.loads(data_str)
                                chunk = parsed['choices'][0]['delta'].get('content', '')
                                if chunk:
                                    full_assistant_reply += chunk
                                    yield json.dumps({"delta": chunk}, ensure_ascii=False) + "\n"
                            except Exception:
                                continue
            else:
                yield json.dumps({"delta": "Yanıt alınamadı, lütfen tekrar deneyin."}, ensure_ascii=False) + "\n"

            # Yapay zekanın cevabını veritabanına kaydet
            if full_assistant_reply:
                with app.app_context():
                    assistant_msg_record = ChatMessage(user_id=current_user.id, role="assistant", content=full_assistant_reply)
                    db.session.add(assistant_msg_record)
                    db.session.commit()

            yield json.dumps({"done": True}) + "\n"

        except Exception as e:
            yield json.dumps({"delta": f"Hata oluştu: {str(e)}"}, ensure_ascii=False) + "\n"
            yield json.dumps({"done": True}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson; charset=utf-8")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
