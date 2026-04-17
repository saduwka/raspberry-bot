#!/usr/bin/env python3
"""
Интерактивный чат с DeepSeek для доработки бота
"""
import requests
import os
import sys

API_KEY = "sk-..."  # твой ключ с platform.deepseek.com
API_URL = "https://api.deepseek.com/chat/completions"

class DeepSeekChat:
    def __init__(self):
        self.history = []
        self.loaded_files = {}
    
    def load_file(self, filepath):
        """Загрузить файл в контекст"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                self.loaded_files[filepath] = content
                print(f"✓ Загружен: {filepath} ({len(content)} символов)")
                return True
        except Exception as e:
            print(f"✗ Ошибка: {e}")
            return False
    
    def send_message(self, user_input):
        """Отправить сообщение"""
        # Добавляем файлы в первое сообщение если есть
        if self.loaded_files and len(self.history) == 0:
            context = "Вот мои файлы:\n\n"
            for path, content in self.loaded_files.items():
                context += f"--- {path} ---\n```python\n{content}\n```\n\n"
            context += user_input
            user_input = context
        
        self.history.append({"role": "user", "content": user_input})
        
        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer sk-619076f064b64bc58e732bd99e1f2349"},
                json={
                    "model": "deepseek-chat",
                    "messages": self.history,
                    "temperature": 0.7,
                    "stream": True  # Потоковый вывод
                },
                timeout=60,
                stream=True
            )
            
            print("\n🤖 DeepSeek: ", end="", flush=True)
            full_response = ""
            
            for line in response.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        data = line[6:]
                        if data == '[DONE]':
                            break
                        try:
                            import json
                            chunk = json.loads(data)
                            if 'choices' in chunk and len(chunk['choices']) > 0:
                                delta = chunk['choices'][0].get('delta', {})
                                content = delta.get('content', '')
                                if content:
                                    print(content, end="", flush=True)
                                    full_response += content
                        except:
                            pass
            
            print("\n")
            self.history.append({"role": "assistant", "content": full_response})
            return full_response
            
        except Exception as e:
            print(f"\n✗ Ошибка API: {e}")
            self.history.pop()  # Убираем последний user message
            return None
    
    def show_help(self):
        """Показать справку"""
        print("""
╔══════════════════════════════════════════╗
║      DeepSeek Chat - Команды             ║
╚══════════════════════════════════════════╝

  /load <файл>   - загрузить файл в контекст
  /files         - показать загруженные файлы
  /clear         - очистить историю
  /help          - эта справка
  /exit          - выход
  
Просто пиши вопросы/промпты напрямую!
""")

def main():
    chat = DeepSeekChat()
    
    print("""
╔═══════════════════════════════════════════╗
║   DeepSeek Chat для доработки бота        ║
║   Напиши /help для списка команд          ║
╚═══════════════════════════════════════════╝
""")
    
    # Автозагрузка файлов из аргументов
    if len(sys.argv) > 1:
        for filepath in sys.argv[1:]:
            chat.load_file(filepath)
    
    while True:
        try:
            user_input = input("\n💬 Ты: ").strip()
            
            if not user_input:
                continue
            
            # Команды
            if user_input == '/exit':
                print("Пока! 👋")
                break
            
            elif user_input == '/help':
                chat.show_help()
            
            elif user_input == '/clear':
                chat.history = []
                chat.loaded_files = {}
                print("✓ История очищена")
            
            elif user_input == '/files':
                if chat.loaded_files:
                    print("\nЗагруженные файлы:")
                    for path in chat.loaded_files.keys():
                        print(f"  • {path}")
                else:
                    print("Нет загруженных файлов")
            
            elif user_input.startswith('/load '):
                filepath = user_input[6:].strip()
                chat.load_file(filepath)
            
            else:
                # Обычное сообщение
                chat.send_message(user_input)
        
        except KeyboardInterrupt:
            print("\n\nПока! 👋")
            break
        except Exception as e:
            print(f"\n✗ Ошибка: {e}")

if __name__ == "__main__":
    main()
