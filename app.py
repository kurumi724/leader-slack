import logging
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import os
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime
import json

# ログ設定
logging.basicConfig(level=logging.INFO)

# 環境変数の読み込み
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Slack API設定
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Google Sheets API設定
SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_ID')
MESSAGE_HISTORY_SHEET_ID = os.getenv('MESSAGE_HISTORY_SHEET_ID')
MESSAGE_HISTORY_SHEET_NAME = os.getenv('MESSAGE_HISTORY_SHEET_NAME')
GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_SHEETS_CREDENTIALS')

# 固定の表示名とアイコンURL
FIXED_USERNAME = "こらしょ"
FIXED_ICON_URL = "https://i.imgur.com/ZwLZQOy.png"

def get_channels_from_sheet():
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
    
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    
    service = build('sheets', 'v4', credentials=creds)
    
    RANGE_NAME = 'Colab）チャンネルID!M2:O'  # 範囲をO列まで拡張
    
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
    values = result.get('values', [])
    
    return {row[1]: {'name': row[0], 'user_id': row[2] if len(row) > 2 else None} for row in values} if values else {}

def add_message_to_sheet(timestamp, channel_id, channel_name, message):
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
    
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    
    service = build('sheets', 'v4', credentials=creds)
    
    RANGE_NAME = f'{MESSAGE_HISTORY_SHEET_NAME}!A:E'  # 1列追加
    
    # タイムスタンプを文字列として保存（小数点以下も含む）
    # シングルクォートを追加してテキスト形式で確実に保存
    unix_timestamp = f"'{timestamp}"
    display_timestamp = datetime.fromtimestamp(float(timestamp)).strftime("%m/%d %H:%M")
    
    values = [[display_timestamp, channel_name, message, unix_timestamp, channel_id]]
    body = {'values': values}
    
    try:
        result = service.spreadsheets().values().append(
            spreadsheetId=MESSAGE_HISTORY_SHEET_ID, range=RANGE_NAME,
            valueInputOption='USER_ENTERED', body=body).execute()
        app.logger.info(f"{result.get('updates').get('updatedCells')} セルが追加されました。")
        return True
    except Exception as e:
        app.logger.error(f"エラーが発生しました: {e}")
        return False

def update_sheet_completion(timestamp, channel_id, completion_time):
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
    
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    
    service = build('sheets', 'v4', credentials=creds)
    
    RANGE_NAME = f'{MESSAGE_HISTORY_SHEET_NAME}!A:G'
    
    result = service.spreadsheets().values().get(
        spreadsheetId=MESSAGE_HISTORY_SHEET_ID, range=RANGE_NAME).execute()
    rows = result.get('values', [])
    
    app.logger.info(f"Spreadsheet content: {rows}")
    
    # ヘッダー行をスキップ
    if len(rows) > 0:
        header = rows[0]
        app.logger.info(f"Header row: {header}")
        rows = rows[1:]
    
    # completion_time を文字列に変換
    formatted_completion_time = completion_time.strftime("%m/%d %H:%M")
    
    for i, row in enumerate(rows, start=2):  # start=2 because we're skipping the header row
        app.logger.info(f"Checking row: {row}")
        if len(row) >= 5:
            try:
                row_timestamp = row[3].strip().strip("'")
                row_channel_id = row[4].strip()
                app.logger.info(f"Comparing: '{row_timestamp}' == '{timestamp}' and '{row_channel_id}' == '{channel_id}'")
                if abs(float(row_timestamp) - float(timestamp)) < 0.001 and row_channel_id == channel_id:
                    update_range = f'{MESSAGE_HISTORY_SHEET_NAME}!F{i}:G{i}'
                    try:
                        service.spreadsheets().values().update(
                            spreadsheetId=MESSAGE_HISTORY_SHEET_ID,
                            range=update_range,
                            valueInputOption='USER_ENTERED',
                            body={'values': [['完了', formatted_completion_time]]}
                        ).execute()
                        app.logger.info(f"Updated completion status for channel ID: {channel_id}, timestamp: {timestamp}, completion time: {formatted_completion_time}")
                        return True
                    except Exception as e:
                        app.logger.error(f"Error updating sheet: {e}")
                        return False
            except ValueError as e:
                app.logger.error(f"Error processing row {i}: {e}")
                continue  # Skip this row and continue with the next
    
    app.logger.warning(f"No matching row found for channel ID: {channel_id}, timestamp: {timestamp}")
    return False

# Slackメッセージを更新する関数を追加
def update_slack_message(channel, timestamp, text, blocks):
    try:
        response = slack_client.chat_update(
            channel=channel,
            ts=timestamp,
            text=text,
            blocks=blocks
        )
        return response
    except SlackApiError as e:
        app.logger.error(f"Error updating Slack message: {e}")
        return None

def send_slack_message(channel_id, message, user_id):
    try:
        mention = f"<@{user_id}>" if user_id else ""
        text = f"{mention}へ\n新しいクエストが登場しました\n\n*【ＭＩＳＳＩＯＮ】*\n{message}"
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "完了"},
                        "value": "complete",
                        "action_id": "complete_button"
                    }
                ]
            }
        ]
        response = slack_client.chat_postMessage(
            channel=channel_id,
            text=text.replace('\n', ' \n'),  # Slackの仕様に合わせて改行の前にスペースを追加
            blocks=blocks,
            username=FIXED_USERNAME,
            icon_url=FIXED_ICON_URL
        )
        return response
    except SlackApiError as e:
        app.logger.error(f"Error sending message: {e}")
        return None

@app.route('/', methods=['GET', 'POST'])
def home():
    channels = get_channels_from_sheet()
    if request.method == 'POST':
        channel_id = request.form['channel']
        message = request.form['message']
        user_id = channels[channel_id].get('user_id')  # チャンネル情報からuser_idを取得
        
        response = send_slack_message(channel_id, message, user_id)
        
        if response and response['ok']:
            timestamp = response['ts']
            channel_name = channels[channel_id]['name']
            if add_message_to_sheet(timestamp, channel_id, channel_name, message):
                result_message = "メッセージが正常に送信され、履歴へ記録されました。"
            else:
                result_message = "メッセージは送信されましたが、履歴への記録に失敗しました。"
        else:
            result_message = "メッセージの送信に失敗しました。"
        
        return render_template('result.html', message=result_message)
    
    return render_template('home.html', channels=channels)

@app.route('/slack/actions', methods=['POST'])
def handle_slack_actions():
    payload = json.loads(request.form["payload"])
    app.logger.info(f"Received payload: {payload}")

    if payload["type"] == "block_actions":
        action = payload["actions"][0]
        if action["action_id"] == "complete_button":
            timestamp = payload["message"]["ts"]
            channel_id = payload["channel"]["id"]
            completion_time = datetime.now()

            app.logger.info(f"Completing task for channel ID: {channel_id}, timestamp: {timestamp}, completion time: {completion_time.strftime('%m/%d %H:%M')}")
            
            # デバッグ: 元のメッセージの構造を確認
            app.logger.info(f"Original message structure: {json.dumps(payload['message'], indent=2)}")
            
            if update_sheet_completion(timestamp, channel_id, completion_time):
                # メッセージを更新して完了ボタンを無効化
                original_text = payload["message"]["blocks"][0]["text"]["text"]
                # デバッグ: 元のテキストを確認
                app.logger.info(f"Original text: {repr(original_text)}")
                
                updated_text = f"{original_text}\n\n[完了済み - {completion_time.strftime('%m/%d %H:%M')}]"
                # デバッグ: 更新後のテキストを確認
                app.logger.info(f"Updated text: {repr(updated_text)}")
                
                updated_blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": updated_text
                        }
                    }
                ]
                response = update_slack_message(channel_id, timestamp, updated_text.replace('\n', ' \n'), updated_blocks)
                # デバッグ: 更新レスポンスを確認
                if response and response['ok']:
                    app.logger.info(f"Message updated successfully")
                else:
                    app.logger.error(f"Failed to update message: {response.get('error', 'Unknown error')}")
                
                return jsonify({"response": "タスクが完了としてマークされました。"}), 200
            else:
                app.logger.error(f"Failed to update sheet for channel ID: {channel_id}, timestamp: {timestamp}")
                return jsonify({"error": "タスクの更新に失敗しました。"}), 500

    return jsonify({"response": "Action received"}), 200

if __name__ == '__main__':
    app.run(debug=True)