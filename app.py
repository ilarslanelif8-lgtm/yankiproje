import os
import json
import logging
import requests
import base64
from io import BytesIO
from PIL import Image
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai

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
MODEL_NAME = "Gemini-Flash"

SYSTEM_PROMPT = (
    "Sen 'Yankı' adında samimi, akıllı ve Türkçe konuşan bir yapay zeka asistansın.\n"
    "Kullanıcının sorularına net, yardımsever ve içten cevap ver."
)

# Gemini API Kurulumu
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

def get_live_market_data():
    """Canlı altın/finans verilerini çeker"""
    try:
        res = requests.get("https://api.genelpara.com/embed/altin.json", timeout=4)
        if res.status_code == 200:
            data = res.json()
            ga, c = data.get("GA", {}), data.get("C", {})
            return (
                f"\n[CANLI BİLGİ - BUGÜNÜN FİYATLARI]: "
                f"Gram Altın Alış: {ga.get('alis')} TL, Satış: {ga.get('satis')} TL | "
                f"Çeyrek Altın Alış: {c.get('alis')} TL, Satış: {c.get('satis')} TL"
            )
    except Exception:
        pass
    return ""

@app.route("/")
@login_required
def index():
    history_messages = ChatMessage.query.filter_by(user_id=current_user.id).order_by(ChatMessage.timestamp.asc()).all()
    return render_template("index.html", model_name=MODEL_NAME, assistant_name=ASSISTANT_NAME, user=current_user, history=history_messages)

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(url_for('index'))
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
    data = request.get_json(force=True) or {}
    user_message = (data.get("message") or "").strip()
    image_base64 = data.get("image")

    if not user_message and not image_base64:
        return jsonify({"error": "Boş mesaj verilemez."}), 400

    # Veritabanına Kaydet
    user_msg_record = ChatMessage(user_id=current_user.id, role="user", content=user_message, image_url=image_base64)
    db.session.add(user_msg_record)
    db.session.commit()

    def generate():
        if not GEMINI_KEY:
            yield json.dumps({"delta": "GEMINI_API_KEY eksik! Lütfen Render'a ekleyin."}, ensure_ascii=False) + "\n"
            yield json.dumps({"done": True}) + "\n"
            return

        try:
            # Gemini Modelini Çağır
            model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=SYSTEM_PROMPT)
            
            contents = []
            
            # Altın/Finans Sorusu Kontrolü
            msg_lower = user_message.lower()
            if any(k in msg_lower for k in ["altın", "altin", "gram", "çeyrek", "fiyat"]):
                live_info = get_live_market_data()
                if live_info:
                    user_message += live_info

            # Görsel Varsa PIL Image Formatına Çevirip Ekle
            if image_base64 and "," in image_base64:
                header, encoded = image_base64.split(",", 1)
                img_data = base64.b64decode(encoded)
                pil_img = Image.open(BytesIO(img_data))
                contents.append(pil_img)

            if user_message:
                contents.append(user_message)
            elif image_base64:
                contents.append("Bu görseli detaylıca analiz et ve açıkla.")

            # Cevabı Canlı (Stream) Akışla Al
            response = model.generate_content(contents, stream=True)
            full_reply = ""

            for chunk in response:
                if chunk.text:
                    full_reply += chunk.text
                    yield json.dumps({"delta": chunk.text}, ensure_ascii=False) + "\n"

            # Asistan Cevabını Veritabanına Kaydet
            if full_reply:
                with app.app_context():
                    assistant_msg = ChatMessage(user_id=current_user.id, role="assistant", content=full_reply)
                    db.session.add(assistant_msg)
                    db.session.commit()

            yield json.dumps({"done": True}) + "\n"

        except Exception as e:
            yield json.dumps({"delta": f"\nHata oluştu: {str(e)}"}, ensure_ascii=False) + "\n"
            yield json.dumps({"done": True}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson; charset=utf-8")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
