#!/usr/bin/env python3
"""
DeepSeek CLI для доработки бота
Использование:
  ./deepseek_helper.py "добавь логирование в функцию X"
  ./deepseek_helper.py "исправь баг" --file ai_utils.py
"""
import requests
import sys
import argparse

API_KEY = "sk-..."  # получи на https://platform.deepseek.com/

def ask_deepseek(prompt, files=None, max_tokens=4000):
    """Отправить запрос в DeepSeek API"""
    
    content = prompt
    
    # Если переданы файлы, добавляем их содержимое
    if files:
        for file_path in files:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    code = f.read()
                content += f"\n\n--- Файл: {file_path} ---\n```python\n{code}\n```"
            except Exception as e:
                print(f"Ошибка чтения {file_path}: {e}")
    
    response = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={
            "Authorization": f"Bearer sk-619076f064b64bc58e732bd99e1f2349",
            "Content-Type": "application/json"
        },
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.7,
            "max_tokens": max_tokens
        },
        timeout=30
    )
    
    if response.status_code == 200:
        return response.json()['choices'][0]['message']['content']
    else:
        return f"Ошибка API: {response.status_code}\n{response.text}"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='DeepSeek CLI helper')
    parser.add_argument('prompt', help='Промпт для DeepSeek')
    parser.add_argument('-f', '--file', action='append', help='Файл(ы) для контекста')
    parser.add_argument('-t', '--tokens', type=int, default=4000, help='Max tokens')
    
    args = parser.parse_args()
    
    result = ask_deepseek(args.prompt, args.file, args.tokens)
    print(result)
