import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

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
    "Sen 'Yankı' adında akıllı ve Türkçe konuşan bir yapay zeka asistansın. "
    "Sana sunulan CANLI BİLGİLERİ doğrudan kullanarak altın, döviz, haber ve hava durumu sorularına %100 GERÇEK VE GÜNCEL rakamlarla cevap ver. "
    "Asla eski yılların fiyatlarını uydurma."
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
    """Canlı altın ve döviz kurlarını direkt finans servisinden çeker"""
    try:
        url = "https://api.genelpara.com/embed/altin.json"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            ga = data.get("GA", {})
            c = data.get("C", {})
            y = data.get("Y", {})
            t = data.get("T", {})
            return (
                f"CANLI FİNANS VERİLERİ (ANLIK):\n"
                f"- Gram Altın Alış: {ga.get('alis')} TL | Satış: {ga.get('satis')} TL (Değişim: %{ga.get('degisim')})\n"
                f"- Çeyrek Altın Alış: {c.get('alis')} TL | Satış: {c.get('satis')} TL\n"
                f"- Yarım Altın Alış: {y.get('alis')} TL | Satış: {y.get('satis')} TL\n"
                f"- Tam Altın Alış: {t.get('alis')} TL | Satış: {t.get('satis')} TL"
            )
    except Exception as e:
        logging.error(f"Finans servisi hatası: {e}")
    return ""

def search_web_fallback(query):
    """Genel aramalar için yedek arama servisi"""
    try:
        url = f"https://html.duckduckgo.com/html/?q={query}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            results = [a.get_text() for a in soup.find_all('a', class_='result__snippet')[:3]]
            return "\n".join(results)
    except Exception:
        pass
    return ""

@app.route("/")
@login_required
def index():
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
        login_user(new_user, remember=True)
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
            login_user(user, remember=True)
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

    user_msg_record = ChatMessage(user_id=current_user.id, role="user", content=user_message, image_url=image_base64)
    db.session.add(user_msg_record)
    db.session.commit()

    recent_db_messages = ChatMessage.query.filter_by(user_id=current_user.id).order_by(ChatMessage.timestamp.desc()).limit(8).all()
    recent_db_messages.reverse()

    api_key = os.environ.get("GROQ_API_KEY", "")

    search_context = ""
    # Soru altın/döviz ile ilgiliyse direkt anlık borsa verisini çek
    msg_lower = user_message.lower()
    if any(k in msg_lower for k in ["altın", "altin", "gram", "çeyrek", "ceyrek", "dolar", "euro"]):
        live_data = get_live_market_data()
        if live_data:
            search_context = f"\n\n[{live_data}]\n\nBu verileri kullanarak kullanıcıya kesin ve net cevap ver."
    elif user_message and not image_base64:
        web_data = search_web_fallback(user_message)
        if web_data:
            search_context = f"\n\n[İNTERNET ARAMA SONUÇLARI]:\n{web_data}"

    messages = [{"role": "system", "content": SYSTEM_PROMPT + search_context}]
    for m in recent_db_messages[:-1]:
        if m.content:
            messages.append({"role": m.role, "content": m.content})

    if image_base64:
        model_name = "llama-3.2-11b-vision-preview"
        content_payload = [
            {"type": "text", "text": user_message if user_message else "Görseli incele ve açıkla."},
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
