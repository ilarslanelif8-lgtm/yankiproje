import os
import json
import logging
import requests
import base64
import re
from io import BytesIO
from PIL import Image
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# --- YENİ: eski "google.generativeai" SDK'sı yerine güncel "google-genai" SDK'sı ---
# (pip install google-genai)  -- eski "google-generativeai" paketi artık deprecated.
from google import genai
from google.genai import types

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

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# Yeni SDK'da configure() yok; bir Client nesnesi oluşturuluyor.
gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

# --- YENİ: Google Search grounding tool'u ---
# Bu tool açıkken Gemini, cevap vermeden önce gerektiğinde otomatik olarak
# gerçek zamanlı Google araması yapıyor (haberler, güncel bilgiler, "kim/ne zaman" vs.)
# Model her mesajda zorla arama yapmıyor; ihtiyaç olduğuna kendisi karar veriyor.
SEARCH_TOOL = types.Tool(google_search=types.GoogleSearch())

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
    """Canlı altın/finans verisi çeker (Google Search'e ek, kesin rakam garantisi için)"""
    try:
        res = requests.get("https://api.genelpara.com/embed/altin.json", timeout=4)
        if res.status_code == 200:
            data = res.json()
            ga, c = data.get("GA", {}), data.get("C", {})
            return (
                f"\n[CANLI PİYASA FİYATLARI]: "
                f"Gram Altın Alış: {ga.get('alis')} TL, Satış: {ga.get('satis')} TL | "
                f"Çeyrek Altın Alış: {c.get('alis')} TL, Satış: {c.get('satis')} TL"
            )
    except Exception:
        pass
    return ""

def clean_thinking_process(text):
    """Gemini'nin dışarı sızdırdığı iç düşünce süreçlerini ve İngilizce notları temizler."""
    if not text:
        return ""
    cleaned = re.sub(r'(\*|\-)?\s*(User question|Context|Persona constraints|Persona|Step \d|Drafting|Greeting|Self-Correction).*?\n', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\* .*?\n', '', cleaned)
    return cleaned.strip()

def format_grounding_sources(response):
    """Gemini'nin arama sırasında kullandığı kaynakları küçük bir liste halinde döndürür."""
    try:
        candidate = response.candidates[0]
        gm = getattr(candidate, "grounding_metadata", None)
        if not gm or not getattr(gm, "grounding_chunks", None):
            return ""
        links = []
        for chunk in gm.grounding_chunks:
            web = getattr(chunk, "web", None)
            if web and getattr(web, "uri", None):
                title = getattr(web, "title", None) or web.uri
                links.append(f"- [{title}]({web.uri})")
        if not links:
            return ""
        # aynı kaynağı iki kere göstermeyelim
        unique_links = list(dict.fromkeys(links))[:5]
        return "\n\n**Kaynaklar:**\n" + "\n".join(unique_links)
    except Exception:
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

    # --- DEĞİŞTİ: Artık "genel web araması" istendiği için, Gemini key varsa
    # HER metin mesajı Gemini + Google Search ile işleniyor. Model kendi karar
    # veriyor gerçekten arama yapıp yapmayacağına (her mesajda zorla aramıyor,
    # sadece gerektiğinde). Gemini key yoksa Groq'a (aramasız) düşüyoruz.
    use_gemini = bool(GEMINI_KEY) and (bool(image_base64) or True)

    def generate():
        full_reply = ""

        if use_gemini:
            if not gemini_client:
                yield json.dumps({"delta": "GEMINI_API_KEY bulunamadı."}, ensure_ascii=False) + "\n"
                yield json.dumps({"done": True}) + "\n"
                return

            prompt_text = user_message
            if needs_live_data:
                live_info = get_live_market_data()
                if live_info:
                    prompt_text += live_info

            contents = [prompt_text]

            if image_base64 and "," in image_base64:
                _, encoded = image_base64.split(",", 1)
                img_data = base64.b64decode(encoded)
                pil_img = Image.open(BytesIO(img_data))
                contents.append(pil_img)

            # Hesabındaki aktif Google Gemini modellerini dinamik çekmeye çalışıyoruz:
            candidate_models = []
            try:
                for m in gemini_client.models.list():
                    actions = getattr(m, "supported_actions", None) or []
                    if "generateContent" in actions:
                        candidate_models.append(m.name.replace("models/", ""))
            except Exception:
                pass

            # Dinamik çekilemezse güncel bilinen model isimleri:
            if not candidate_models:
                candidate_models = [
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                    "gemini-1.5-flash",
                ]

            success = False
            last_err = ""

            config = types.GenerateContentConfig(tools=[SEARCH_TOOL])

            for m_name in candidate_models:
                try:
                    response = gemini_client.models.generate_content(
                        model=m_name,
                        contents=contents,
                        config=config,
                    )

                    if response.text:
                        raw_text = response.text
                        clean_text = clean_thinking_process(raw_text)
                        sources = format_grounding_sources(response)
                        full_reply = clean_text + sources
                        yield json.dumps({"delta": clean_text + sources}, ensure_ascii=False) + "\n"

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

                messages = [{"role": "system", "content": "Sen 'Yankı' adında Türkçe konuşan zeki ve kibar bir asistansın."}]
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

