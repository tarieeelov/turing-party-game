from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from typing import Dict, List
import json
import random
import asyncio
import os

from dotenv import load_dotenv
import google.generativeai as genai

# ---------- НАСТРОЙКА GEMINI ----------

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY not found in environment/.env")

genai.configure(api_key=api_key)
# используй ту модель, с которой у тебя уже работало
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

# ---------- FASTAPI ПРИЛОЖЕНИЕ ----------

app = FastAPI()

BOT_CANDIDATE_NAMES = ["Alex", "Sam", "Taylor", "Jordan", "Dana", "Max", "Chris", "Nika"]


class ConnectionManager:
    def __init__(self):
        # rooms_ws: room_id -> {real_name: websocket}
        self.rooms_ws: Dict[str, Dict[str, WebSocket]] = {}

        # room_states: room_id -> {
        #   "humans": [real_name, ...],
        #   "bot_name": str | None,
        #   "aliases": {real_name: "PlayerX"},
        #   "history": [{"alias": "Player1", "text": "..."}, ...],
        #   "voting": {"is_open": bool, "votes": {voter_alias: target_alias}},
        # }
        self.room_states: Dict[str, Dict] = {}

    def _get_or_create_state(self, room_id: str) -> Dict:
        if room_id not in self.room_states:
            self.room_states[room_id] = {
                "humans": [],
                "bot_name": None,
                "aliases": {},
                "history": [],
                "voting": {"is_open": False, "votes": {}},
            }
        else:
            state = self.room_states[room_id]
            state.setdefault("humans", [])
            state.setdefault("bot_name", None)
            state.setdefault("aliases", {})
            state.setdefault("history", [])
            state.setdefault("voting", {"is_open": False, "votes": {}})
        return self.room_states[room_id]

    def _create_bot_name(self, existing_names: List[str]) -> str:
        candidates = [n for n in BOT_CANDIDATE_NAMES if n not in existing_names]
        if candidates:
            return random.choice(candidates)
        return f"Player{len(existing_names) + 1}"

    def _rebuild_aliases(self, room_id: str):
        """
        Пересоздаём случайное отображение real_name -> Player1..N
        для всех участников (люди + бот).
        """
        state = self._get_or_create_state(room_id)

        ids = state["humans"][:]
        bot = state.get("bot_name")
        if bot:
            ids.append(bot)

        if not ids:
            state["aliases"] = {}
            return

        labels = [f"Player{i+1}" for i in range(len(ids))]
        random.shuffle(labels)

        state["aliases"] = {real: label for real, label in zip(ids, labels)}
        print(f"[ALIAS] room {room_id} aliases:", state["aliases"])

    def _add_to_history(self, room_id: str, alias: str, text: str):
        """Добавляем сообщение в историю комнаты (по алиасу)."""
        state = self._get_or_create_state(room_id)
        state["history"].append({"alias": alias, "text": text})
        if len(state["history"]) > 50:
            state["history"] = state["history"][-50:]

    async def connect(self, room_id: str, player_id: str, websocket: WebSocket):
        await websocket.accept()

        if room_id not in self.rooms_ws:
            self.rooms_ws[room_id] = {}

        self.rooms_ws[room_id][player_id] = websocket
        print(f"✅ {player_id} подключился к комнате {room_id}")

        state = self._get_or_create_state(room_id)
        if player_id not in state["humans"]:
            state["humans"].append(player_id)

        # Если уже 3 живых игрока и бота ещё нет — добавляем бота и запускаем раунд
        if len(state["humans"]) == 3 and state["bot_name"] is None:
            all_names = state["humans"][:]
            bot_name = self._create_bot_name(all_names)
            state["bot_name"] = bot_name
            print(f"🤖 Добавлен скрытый бот '{bot_name}' в комнату {room_id}")

            self._rebuild_aliases(room_id)

            bot_alias = state["aliases"].get(bot_name, "New player")
            await self.broadcast_system(
                room_id,
                f"{bot_alias} joined the room."
            )

            # Запускаем таймер раунда на 2 минуты
            asyncio.create_task(self.start_round(room_id))
        else:
            self._rebuild_aliases(room_id)

        alias = self.room_states[room_id]["aliases"].get(player_id, player_id)
        await self.broadcast_system(
            room_id,
            f"{alias} joined the room."
        )
        await self.broadcast_players(room_id)

    def disconnect(self, room_id: str, player_id: str):
        room_ws = self.rooms_ws.get(room_id)
        state = self.room_states.get(room_id)

        if room_ws and player_id in room_ws:
            del room_ws[player_id]
            print(f"❌ {player_id} вышел из комнаты {room_id}")
            if not room_ws:
                del self.rooms_ws[room_id]

        if state and player_id in state.get("humans", []):
            state["humans"].remove(player_id)

        if state and not state["humans"]:
            if room_id in self.room_states:
                del self.room_states[room_id]
        else:
            self._rebuild_aliases(room_id)

    async def broadcast_json(self, room_id: str, payload: dict):
        """Отправить JSON всем участникам комнаты, игнорируя мёртвые сокеты."""
        room_ws = self.rooms_ws.get(room_id)
        if not room_ws:
            return

        message_text = json.dumps(payload, ensure_ascii=False)
        to_remove = []

        for real_id, ws in list(room_ws.items()):
            if ws.client_state != WebSocketState.CONNECTED:
                print(f"[WS] {real_id} в комнате {room_id} уже не CONNECTED, удаляю из списка")
                to_remove.append(real_id)
                continue

            try:
                await ws.send_text(message_text)
            except Exception as e:
                print(f"[WS] ошибка при отправке игроку {real_id} в комнате {room_id}: {e}")
                to_remove.append(real_id)

        for real_id in to_remove:
            room_ws.pop(real_id, None)

    async def broadcast_chat(self, room_id: str, from_player: str, text: str):
        """
        Отправить чат-сообщение всем.
        from_player — внутреннее имя (реальное или имя бота),
        наружу уходит его алиас PlayerX.
        """
        state = self._get_or_create_state(room_id)
        if not state["aliases"]:
            self._rebuild_aliases(room_id)

        alias = state["aliases"].get(from_player, from_player)

        # добавляем в историю
        self._add_to_history(room_id, alias, text)

        await self.broadcast_json(room_id, {
            "type": "chat",
            "from": alias,
            "text": text
        })

    async def broadcast_system(self, room_id: str, text: str):
        """Системное сообщение (подключение/отключение и т.п.)."""
        await self.broadcast_json(room_id, {
            "type": "system",
            "text": text
        })

    async def broadcast_players(self, room_id: str):
        """Отправить актуальный список игроков в комнате (только алиасы)."""
        state = self._get_or_create_state(room_id)
        if not state["aliases"]:
            self._rebuild_aliases(room_id)

        players = list(state["aliases"].values())

        await self.broadcast_json(room_id, {
            "type": "players",
            "players": players
        })

    # ---------- ЛОГИКА РАУНДА И ГОЛОСОВАНИЯ ----------

    async def start_round(self, room_id: str):
        """
        Старт раунда: 2 минуты общения, потом — голосование.
        """
        if room_id not in self.room_states:
            return

        await self.broadcast_system(
            room_id,
            "⏰ Раунд начался! У вас есть 2 минуты на общение, потом начнётся голосование, кто бот."
        )

        try:
            await asyncio.sleep(120)
        except asyncio.CancelledError:
            return

        if room_id not in self.room_states:
            return

        await self.start_voting(room_id)

    async def start_voting(self, room_id: str):
        """Открываем голосование."""
        state = self._get_or_create_state(room_id)
        state["voting"] = {"is_open": True, "votes": {}}

        if not state["aliases"]:
            self._rebuild_aliases(room_id)

        players = list(state["aliases"].values())

        await self.broadcast_json(room_id, {
            "type": "voting_start",
            "players": players,
            "message": "⏰ Время вышло! Голосуйте, кто был ботом. Выберите одного игрока."
        })

    async def register_vote(self, room_id: str, real_voter: str, target_alias: str):
        """
        Регистрируем голос: реальный игрок голосует за alias (PlayerX).
        """
        state = self._get_or_create_state(room_id)
        voting = state.get("voting", {})
        if not voting.get("is_open"):
            return

        if real_voter not in state["humans"]:
            # бот не голосует
            return

        aliases = state["aliases"]
        voter_alias = aliases.get(real_voter, real_voter)

        # Проверяем, что таргет существует среди игроков
        if target_alias not in aliases.values():
            return

        voting["votes"][voter_alias] = target_alias

        await self.broadcast_system(room_id, f"{voter_alias} сделал свой выбор.")

        # Проверяем, все ли люди проголосовали
        human_aliases = [aliases[h] for h in state["humans"] if h in aliases]
        if all(a in voting["votes"] for a in human_aliases):
            await self.finish_voting(room_id)

    async def finish_voting(self, room_id: str):
        """Подводим итоги голосования, считаем голоса и объявляем победу/проигрыш."""
        state = self._get_or_create_state(room_id)
        voting = state.get("voting")
        if not voting:
            return

        votes = voting.get("votes", {})
        aliases = state["aliases"]
        bot_name = state.get("bot_name")
        bot_alias = aliases.get(bot_name, bot_name) if bot_name else "Unknown"

        # Подсчёт голосов
        counts: Dict[str, int] = {}
        for voter_alias, target_alias in votes.items():
            counts[target_alias] = counts.get(target_alias, 0) + 1

        if counts:
            winner_alias, winner_count = max(counts.items(), key=lambda kv: kv[1])
        else:
            winner_alias, winner_count = None, 0

        total_voters = len(votes)
        majority_correct = False
        if winner_alias is not None and total_voters > 0:
            # "Большинство" = строго больше половины
            if winner_alias == bot_alias and winner_count > total_voters / 2:
                majority_correct = True

        result_text = (
            "ПОБЕДА! Большинство угадали бота. 🎉"
            if majority_correct
            else "ПРОИГРЫШ! Большинство не угадали бота. 😈"
        )

        votes_list = [{"voter": v, "target": t} for v, t in votes.items()]

        await self.broadcast_json(room_id, {
            "type": "voting_result",
            "bot": bot_alias,
            "votes": votes_list,
            "counts": counts,
            "winner": winner_alias,
            "majority_correct": majority_correct,
            "result_text": result_text,
        })

        voting["is_open"] = False

    # ---------- ЛОГИКА ОТВЕТОВ БОТА (GEMINI) ----------

    async def maybe_bot_answer(self, room_id: str, from_player: str, text: str):
        """
        Бот отвечает с учётом истории, но не на каждое сообщение:
        - всегда отвечает, если его упомянули или задали вопрос;
        - иначе отвечает с некоторой вероятностью.
        Во время голосования бот уже молчит.
        """
        state = self._get_or_create_state(room_id)
        bot_name = state.get("bot_name")
        if not bot_name:
            return

        # Если уже идёт голосование — бот молчит
        voting = state.get("voting", {})
        if voting.get("is_open"):
            return

        if not state["aliases"]:
            self._rebuild_aliases(room_id)

        from_alias = state["aliases"].get(from_player, from_player)
        bot_alias = state["aliases"].get(bot_name, bot_name)

        text_lower = text.lower()

        mentioned = (
            bot_alias.lower() in text_lower
            or "бот" in text_lower
            or "bot" in text_lower
        )

        is_question = "?" in text

        should_reply = False

        if mentioned or (is_question and from_alias != bot_alias):
            should_reply = True
        else:
            # 50% шанс ответить просто так
            should_reply = random.random() < 0.5

        print(
            f"[BOT DECISION] room={room_id} from={from_alias} "
            f"mentioned={mentioned} question={is_question} reply={should_reply}"
        )

        if not should_reply:
            return

        history = state.get("history", [])
        recent = history[-15:]
        history_lines = [f"{m['alias']}: {m['text']}" for m in recent]
        history_block = "\n".join(history_lines) if history_lines else "(пока нет истории)"

        await asyncio.sleep(random.uniform(0.7, 1.8))

        prompt = f"""
Ты играешь роль обычного человека в групповом чате-игре. 
У всех участников ники вида 'Player1', 'Player2' и т.д.
Один из них — ты ({bot_alias}), но пользователи не должны понять, что ты бот.

Вот недавняя история чата (от старых к новым):
{history_block}

Сейчас новое сообщение от {from_alias}:
\"\"\"{text}\"\"\"

Правила ответа:
- Пиши кратко: 1–2 предложения.
- Общайся естественно, как обычный подросток/студент.
- Не упоминай, что ты ИИ или бот.
- Отвечай по-русски.
- Можешь задавать простые встречные вопросы.
- Старайся учитывать контекст из истории.
- Пиши с ошибками, как в реальном чате.
- Можешь использовать сленг.
- Будь нейтрален, если тебе грубят — груби в ответ, если к тебе вежливы — будь вежлив.

Сформулируй один естественный ответ от лица обычного человека в чате.
"""

        def call_gemini():
            try:
                resp = gemini_model.generate_content(prompt)
                print("Gemini raw response:", resp)
                return (resp.text or "").strip()
            except Exception as e:
                print("Gemini error:", repr(e))
                return ""

        reply = await asyncio.to_thread(call_gemini)

        if not reply:
            reply = f"Интересно, {from_alias}. Расскажи ещё 🙂"

        await self.broadcast_chat(room_id, bot_name, reply)


manager = ConnectionManager()

# ---------- HTML + JS ФРОНТ ----------

html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <title>Turing Party Chat</title>
    <style>
        :root {
            --bg-main: #050816;
            --bg-card: #0f172a;
            --bg-card-soft: #111827;
            --accent: #6366f1;
            --accent-soft: rgba(99, 102, 241, 0.15);
            --accent-strong: #4f46e5;
            --text-main: #e5e7eb;
            --text-muted: #9ca3af;
            --border-subtle: #1f2937;
            --danger: #f97373;
            --success: #22c55e;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            padding: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text",
                "Segoe UI", sans-serif;
            background: radial-gradient(circle at top, #1d1e3b 0, #050816 55%, #020617 100%);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        #app {
            width: 100%;
            max-width: 1120px;
            margin: 24px;
            background: rgba(15, 23, 42, 0.95);
            border-radius: 18px;
            border: 1px solid rgba(148, 163, 184, 0.2);
            box-shadow:
                0 22px 60px rgba(15, 23, 42, 0.8),
                0 0 0 1px rgba(15, 23, 42, 0.9);
            overflow: hidden;
        }

        /* HEADER */

        #header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 18px 22px;
            border-bottom: 1px solid var(--border-subtle);
            background: linear-gradient(
                90deg,
                rgba(15, 23, 42, 0.95),
                rgba(37, 99, 235, 0.12),
                rgba(109, 40, 217, 0.12)
            );
        }

        .logo-block {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .logo-circle {
            width: 34px;
            height: 34px;
            border-radius: 999px;
            background: radial-gradient(circle at 30% 0%, #a855f7, #6366f1);
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 18px;
            color: #f9fafb;
            box-shadow: 0 0 0 1px rgba(148, 163, 184, 0.35);
        }

        .logo-text-main {
            font-weight: 600;
            letter-spacing: 0.02em;
            font-size: 16px;
        }

        .logo-text-sub {
            font-size: 12px;
            color: var(--text-muted);
        }

        #top-controls {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .field-inline {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.7);
            border: 1px solid var(--border-subtle);
        }

        .field-inline label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-muted);
        }

        .field-inline input {
            background: transparent;
            border: none;
            outline: none;
            color: var(--text-main);
            font-size: 13px;
            padding: 2px 4px;
        }

        .pill-btn {
            border-radius: 999px;
            border: none;
            padding: 8px 16px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            background: linear-gradient(135deg, var(--accent), var(--accent-strong));
            color: #f9fafb;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            box-shadow:
                0 8px 18px rgba(99, 102, 241, 0.4),
                0 0 0 1px rgba(129, 140, 248, 0.5);
            transition: transform 0.07s ease, box-shadow 0.07s ease, opacity 0.07s ease;
        }

        .pill-btn:hover {
            transform: translateY(-1px);
            box-shadow:
                0 10px 22px rgba(99, 102, 241, 0.6),
                0 0 0 1px rgba(129, 140, 248, 0.7);
        }

        .pill-btn:active {
            transform: translateY(1px) scale(0.99);
            box-shadow:
                0 4px 10px rgba(99, 102, 241, 0.35),
                0 0 0 1px rgba(129, 140, 248, 0.6);
        }

        .pill-btn:disabled {
            opacity: 0.6;
            cursor: default;
            box-shadow: none;
        }

        /* LAYOUT */

        #main {
            display: grid;
            grid-template-columns: minmax(0, 2.2fr) minmax(0, 1.2fr);
            gap: 16px;
            padding: 16px 18px 18px;
        }

        @media (max-width: 880px) {
            #main {
                grid-template-columns: 1fr;
            }
        }

        .card {
            border-radius: 14px;
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            padding: 12px 12px 10px;
        }

        .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 8px;
        }

        .card-title {
            font-size: 13px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-muted);
        }

        .badge {
            font-size: 11px;
            border-radius: 999px;
            padding: 3px 8px;
            background: rgba(148, 163, 184, 0.12);
            color: var(--text-muted);
        }

        #status {
            font-size: 12px;
            margin-top: 6px;
            color: var(--text-muted);
        }

        #status.connected {
            color: var(--success);
        }

        #status.disconnected {
            color: var(--danger);
        }

        /* CHAT */

        #chatCard {
            display: flex;
            flex-direction: column;
            height: 430px;
        }

        #messages {
            flex: 1;
            border-radius: 10px;
            border: 1px solid var(--border-subtle);
            background: radial-gradient(circle at top left, #111827, #020617);
            padding: 8px 10px;
            overflow-y: auto;
            font-size: 13px;
        }

        .system-msg {
            color: var(--text-muted);
            font-style: italic;
            margin: 4px 0;
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .system-msg::before {
            content: "●";
            font-size: 6px;
            color: rgba(148, 163, 184, 0.7);
        }

        .chat-msg {
            margin: 3px 0;
            padding: 4px 6px;
            border-radius: 6px;
            background: rgba(31, 41, 55, 0.8);
        }

        .chat-msg span.from {
            font-weight: 500;
            color: #e5e7eb;
        }

        .chat-msg span.text {
            color: #d1d5db;
        }

        #inputRow {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 8px;
        }

        #msgInput {
            flex: 1;
            border-radius: 999px;
            border: 1px solid var(--border-subtle);
            background: var(--bg-card-soft);
            color: var(--text-main);
            padding: 8px 12px;
            font-size: 13px;
            outline: none;
        }

        #msgInput::placeholder {
            color: var(--text-muted);
        }

        #sendBtn {
            padding: 8px 14px;
            border-radius: 999px;
            border: none;
            background: var(--accent-soft);
            color: var(--accent);
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        #sendBtn:hover {
            background: rgba(129, 140, 248, 0.25);
        }

        #sendBtn:disabled {
            opacity: 0.5;
            cursor: default;
        }

        /* RIGHT COLUMN */

        #playersCard {
            margin-bottom: 10px;
        }

        #playersList {
            list-style: none;
            margin: 6px 0 0;
            padding: 0;
            max-height: 150px;
            overflow-y: auto;
            font-size: 13px;
        }

        #playersList li {
            padding: 4px 6px;
            border-radius: 8px;
            margin-bottom: 3px;
            background: rgba(15, 23, 42, 0.8);
            border: 1px solid rgba(31, 41, 55, 0.9);
        }

        #infoCard p {
            font-size: 12px;
            color: var(--text-muted);
            margin: 6px 0;
        }

        #infoCard p strong {
            color: var(--text-main);
            font-weight: 500;
        }

        /* VOTING */

        #votingBlock {
            display: none;
            margin-top: 6px;
            border-radius: 12px;
            border: 1px solid rgba(55, 65, 81, 0.9);
            background: radial-gradient(circle at top, #111827, #020617);
            padding: 8px 8px 10px;
        }

        #votingBlock h3 {
            margin: 0 0 4px;
            font-size: 13px;
        }

        #votingMessage {
            font-size: 12px;
            color: var(--text-muted);
            margin: 0 0 6px;
        }

        #votingOptions {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }

        .vote-btn {
            border-radius: 999px;
            border: 1px solid rgba(75, 85, 99, 0.9);
            padding: 5px 10px;
            background: rgba(15, 23, 42, 0.9);
            color: var(--text-main);
            font-size: 12px;
            cursor: pointer;
        }

        .vote-btn:hover {
            border-color: var(--accent);
            background: rgba(55, 65, 81, 0.9);
        }

        .hint-text {
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 4px;
        }
    </style>
</head>
<body>
<div id="app">
    <div id="header">
        <div class="logo-block">
            <div class="logo-circle">T</div>
            <div>
                <div class="logo-text-main">Turing Party</div>
                <div class="logo-text-sub">Guess who’s the bot 🤖</div>
            </div>
        </div>
        <div id="top-controls">
            <div class="field-inline">
                <label for="roomId">Room</label>
                <input id="roomId" type="text" value="room1" />
            </div>
            <div class="field-inline">
                <label for="playerName">Name</label>
                <input id="playerName" type="text" value="Player1" />
            </div>
            <button id="connectBtn" class="pill-btn" onclick="connectWS()">
                <span>Connect</span> <span>⬤</span>
            </button>
        </div>
    </div>

    <div id="main">
        <!-- CHAT COLUMN -->
        <div class="card" id="chatCard">
            <div class="card-header">
                <div class="card-title">Room chat</div>
                <div class="badge">Hidden bot game</div>
            </div>

            <div id="messages"></div>

            <div id="inputRow">
                <input id="msgInput" type="text" placeholder="Напиши сообщение..." />
                <button id="sendBtn" onclick="sendMessage()">Send ⏎</button>
            </div>
            <div id="status" class="disconnected">Not connected</div>
        </div>

        <!-- SIDE COLUMN -->
        <div>
            <div class="card" id="playersCard">
                <div class="card-header">
                    <div class="card-title">Players</div>
                    <div class="badge">Live room</div>
                </div>
                <ul id="playersList"></ul>
                <p class="hint-text">
                    Слева вы видите только ники (Player1, Player2...). Среди них спрятан бот.
                </p>
            </div>

            <div class="card" id="infoCard">
                <div class="card-header">
                    <div class="card-title">How it works</div>
                </div>
                <p><strong>1.</strong> Подключитесь к комнате и начните переписку.</p>
                <p><strong>2.</strong> Когда будет достаточно игроков, в комнату зайдёт скрытый бот.</p>
                <p><strong>3.</strong> Общайтесь, наблюдайте за стилем сообщений.</p>
                <p><strong>4.</strong> Через 2 минуты начнётся голосование — выберите, кто, по-вашему, бот.</p>

                <div id="votingBlock">
                    <h3>Vote: Who is the bot?</h3>
                    <p id="votingMessage"></p>
                    <div id="votingOptions"></div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    let ws = null;

    function setStatus(text, isConnected) {
        const statusEl = document.getElementById("status");
        statusEl.innerText = text;
        statusEl.classList.toggle("connected", !!isConnected);
        statusEl.classList.toggle("disconnected", !isConnected);

        const sendBtn = document.getElementById("sendBtn");
        const connectBtn = document.getElementById("connectBtn");

        sendBtn.disabled = !isConnected;
        if (isConnected) {
            connectBtn.innerHTML = "<span>Connected</span> <span>●</span>";
        } else {
            connectBtn.innerHTML = "<span>Connect</span> <span>⬤</span>";
        }
    }

    function connectWS() {
        const roomId = document.getElementById("roomId").value.trim();
        const playerName = document.getElementById("playerName").value.trim();

        if (!roomId || !playerName) {
            alert("Введите Room и Name");
            return;
        }

        if (ws && ws.readyState === WebSocket.OPEN) {
            alert("Вы уже подключены");
            return;
        }

        const loc = window.location;
        const wsProtocol = loc.protocol === "https:" ? "wss" : "ws";
        const wsBase = `${wsProtocol}://${loc.host}`;
        const url = `${wsBase}/ws/${roomId}/${encodeURIComponent(playerName)}`;

        ws = new WebSocket(url);

        ws.onopen = function() {
            setStatus("✅ Connected to room " + roomId + " as " + playerName, true);
        };

        ws.onclose = function() {
            setStatus("❌ Disconnected", false);
        };

        ws.onerror = function() {
            setStatus("⚠️ Connection error", false);
        };

        ws.onmessage = function(event) {
            const data = JSON.parse(event.data);
            const messagesDiv = document.getElementById("messages");

            if (data.type === "chat") {
                const p = document.createElement("div");
                p.className = "chat-msg";
                const fromSpan = document.createElement("span");
                fromSpan.className = "from";
                fromSpan.innerText = data.from + ": ";
                const textSpan = document.createElement("span");
                textSpan.className = "text";
                textSpan.innerText = data.text;

                p.appendChild(fromSpan);
                p.appendChild(textSpan);
                messagesDiv.appendChild(p);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            } else if (data.type === "system") {
                const p = document.createElement("div");
                p.className = "system-msg";
                p.innerText = data.text;
                messagesDiv.appendChild(p);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            } else if (data.type === "players") {
                const playersList = document.getElementById("playersList");
                playersList.innerHTML = "";
                data.players.forEach(pName => {
                    const li = document.createElement("li");
                    li.innerText = pName;
                    playersList.appendChild(li);
                });
            } else if (data.type === "voting_start") {
                const votingBlock = document.getElementById("votingBlock");
                const votingMessage = document.getElementById("votingMessage");
                const votingOptions = document.getElementById("votingOptions");

                votingBlock.style.display = "block";
                votingMessage.innerText = data.message || "Время вышло! Голосуйте, кто был ботом.";

                votingOptions.innerHTML = "";
                data.players.forEach(pName => {
                    const btn = document.createElement("button");
                    btn.innerText = pName;
                    btn.className = "vote-btn";
                    btn.onclick = function() {
                        sendVote(pName);
                    };
                    votingOptions.appendChild(btn);
                });

                const p = document.createElement("div");
                p.className = "system-msg";
                p.innerText = "🗳 Началось голосование! Нажми на ник, чтобы проголосовать.";
                messagesDiv.appendChild(p);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;

            } else if (data.type === "voting_result") {
                const votingBlock = document.getElementById("votingBlock");
                votingBlock.style.display = "none";

                const p = document.createElement("div");
                p.className = "system-msg";
                p.innerText = "🧾 Результаты голосования: " + (data.result_text || "");
                messagesDiv.appendChild(p);

                const p2 = document.createElement("div");
                p2.className = "system-msg";
                p2.innerText = "🤖 Бот был: " + data.bot;
                messagesDiv.appendChild(p2);

                if (data.votes) {
                    data.votes.forEach(v => {
                        const pv = document.createElement("div");
                        pv.className = "system-msg";
                        pv.innerText = `- ${v.voter} проголосовал за ${v.target}`;
                        messagesDiv.appendChild(pv);
                    });
                }

                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            }
        };
    }

    function sendMessage() {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            alert("Сначала подключись к комнате");
            return;
        }
        const input = document.getElementById("msgInput");
        const text = input.value.trim();
        if (!text) return;
        ws.send(text);
        input.value = "";
    }

    function sendVote(playerAlias) {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            alert("Сначала подключись к комнате");
            return;
        }
        ws.send("/vote " + playerAlias);
    }

    // Отправка по Enter
    document.addEventListener("DOMContentLoaded", () => {
        const input = document.getElementById("msgInput");
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
    });
</script>
</body>
</html>
"""


@app.get("/")
async def get():
    return HTMLResponse(html)


@app.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, player_id: str):
    await manager.connect(room_id, player_id, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            print(f"[{room_id}] {player_id}: {data}")

            # Команда голосования вида: /vote PlayerX
            if data.startswith("/vote"):
                parts = data.split()
                if len(parts) >= 2:
                    target_alias = parts[1]
                    await manager.register_vote(room_id, player_id, target_alias)
                continue

            # Обычное чат-сообщение
            await manager.broadcast_chat(room_id, player_id, data)
            await manager.maybe_bot_answer(room_id, player_id, data)

    except WebSocketDisconnect:
        manager.disconnect(room_id, player_id)
        await manager.broadcast_system(room_id, f"{player_id} left the room.")
        await manager.broadcast_players(room_id)
