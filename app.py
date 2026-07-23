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
MODEL_NAME = "Yankı Hibrit (Gemini + Groq)"

SYSTEM_PROMPT = "Sen Yankı adında yardımcı, kibar ve Türkçe konuşan zeki bir yapay zeka asistansın. Kullanıcının sorularına doğrudan ve net cevap ver."

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
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
    """Canlı altın/finans verisi çeker"""
    try:
        res = requests.get("https://api.genelpara.com/embed/altin.json", timeout=4)
        if res.status_code == 200:
            data = res.json()
            ga, c = data.get("GA", {}), data.get("C", {})
            return (
                f"\n[CANLI BİLGİ - GÜNCEL FİYATLAR]: "
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

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        
        if not email or not password:
            flash("Lütfen tüm alanları doldurun!", "warning")
            return redirect(url_for('register'))

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("Bu e-posta adresi zaten kayıtlı!", "danger")
            return redirect(url_for('register'))

        new_user = User(email=email)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()

        login_user(new_user, remember=True)
        flash("Hesabınız başarıyla oluşturuldu!", "success")
        return redirect(url_for('index'))

    return render_template("register.html")

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
        return jsonify({"error": "Boş mesaj gönderilemez."}), 400

    user_msg_record = ChatMessage(user_id=current_user.id, role="user", content=user_message, image_url=image_base64)
    db.session.add(user_msg_record)
    db.session.commit()

    msg_lower = user_message.lower()
    needs_live_data = any(k in msg_lower for k in ["altın", "altin", "gram", "çeyrek", "fiyat", "dolar", "euro"])

    use_gemini = False
    if image_base64 or needs_live_data:
        use_gemini = True

    def generate():
        full_reply = ""
        
        if use_gemini:
            if not GEMINI_KEY:
                yield json.dumps({"delta": "GEMINI_API_KEY bulunamadı."}, ensure_ascii=False) + "\n"
                yield json.dumps({"done": True}) + "\n"
                return

            contents = []
            if needs_live_data:
                live_info = get_live_market_data()
                if live_info:
                    user_message_with_data = user_message + live_info
                    contents.append(user_message_with_data)
                else:
                    contents.append(user_message)
            elif user_message:
                contents.append(user_message)

            if image_base64 and "," in image_base64:
                _, encoded = image_base64.split(",", 1)
                img_data = base64.b64decode(encoded)
                pil_img = Image.open(BytesIO(img_data))
                contents.append(pil_img)

            if not contents and image_base64:
                contents.append("Görseli detaylıca incele ve açıkla.")

            candidate_models = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-2.0-flash", "gemini-1.5-pro"]
            
            try:
                available_from_api = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
                if available_from_api:
                    candidate_models = available_from_api + candidate_models
            except Exception:
                pass

            success = False
            last_err = ""

            for m_name in candidate_models:
                try:
                    model = genai.GenerativeModel(m_name, system_instruction=SYSTEM_PROMPT)
                    response = model.generate_content(contents, stream=True)
                    
                    for chunk in response:
                        if chunk.text:
                            full_reply += chunk.text
                            yield json.dumps({"delta": chunk.text}, ensure_ascii=False) + "\n"
                    
                    success = True
                    break
                except Exception as e:
                    last_err = str(e)
                    continue

            if not success:
                yield json.dumps({"delta": f"Gemini Bağlantı Hatası: {last_err}"}, ensure_ascii=False) + "\n"

        else:
            if not GROQ_KEY:
                yield json.dumps({"delta": "GROQ_API_KEY bulunamadı."}, ensure_ascii=False) + "\n"
                yield json.dumps({"done": True}) + "\n"
                return

            try:
                recent_msgs = ChatMessage.query.filter_by(user_id=current_user.id).order_by(ChatMessage.timestamp.desc()).limit(6).all()
                recent_msgs.reverse()

                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                for m in recent_msgs[:-1]:
                    if m.content:
                        messages.append({"role": m.role, "content": m.content})
                messages.append({"role": "user", "content": user_message})

                headers = {
                    "Authorization": f"Bearer {GROQ_KEY}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": "llama-3.3-70b-versatile",
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
                                        full_reply += chunk
                                        yield json.dumps({"delta": chunk}, ensure_ascii=False) + "\n"
                                except Exception:
                                    continue
                else:
                    yield json.dumps({"delta": f"Groq Hatası (Kod: {res.status_code})"}, ensure_ascii=False) + "\n"

            except Exception as e:
                yield json.dumps({"delta": f"Groq Bağlantı Hatası: {str(e)}"}, ensure_ascii=False) + "\n"

        if full_reply:
            with app.app_context():
                assistant_msg = ChatMessage(user_id=current_user.id, role="assistant", content=full_reply)
                db.session.add(assistant_msg)
                db.session.commit()

        yield json.dumps({"done": True}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson; charset=utf-8")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
