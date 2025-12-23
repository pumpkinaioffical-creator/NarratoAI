from openai import OpenAI
import re

client = OpenAI(
    base_url="https://api.netmind.ai/inference-api/openai/v1",
    api_key="4b185095432141fe967508323c875979"
)

# 关键点：在 System Prompt 中强制定义输出格式
# 要求模型必须先输出 <thinking>...</thinking>，再输出 <answer>...</answer>
system_prompt = """
You are a deep thinking AI. You are capable of complex reasoning and self-reflection.

Format Requirements:
1. You MUST first perform a deep analysis of the user's request inside <thinking> tags. 
2. Inside the <thinking> section, explore multiple angles, draft potential content, and critique your own ideas.
3. After the thinking block is closed, provide the final response inside <answer> tags.

Example Structure:
<thinking>
- Analyze request: Write an article about X.
- Brainstorming: Idea A, Idea B.
- Draft structure: Intro -> Body -> Conclusion.
</thinking>
<answer>
[Final Article Content Here]
</answer>
"""

print("正在请求模型进行深度思考...")

try:
    chat_completion_response = client.chat.completions.create(
        model="google/gemini-3-pro-preview", 
        messages=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": "Write a 100-word article on Benefits of Open-Source in AI research."
            }
        ],
        max_tokens=4096,
        temperature=0.7, # 稍微提高一点温度，让思考过程更发散
    )

    raw_content = chat_completion_response.choices[0].message.content

    # === 解析并分离显示 ===
    
    # 提取思考部分
    thinking_match = re.search(r'<thinking>(.*?)</thinking>', raw_content, re.DOTALL)
    # 提取回答部分
    answer_match = re.search(r'<answer>(.*?)</answer>', raw_content, re.DOTALL)

    # 如果模型没有严格遵守标签，就打印原始内容
    if not thinking_match and not answer_match:
        print("\n=== 模型未遵循格式，直接输出 ===\n")
        print(raw_content)
    else:
        if thinking_match:
            print("\n" + "="*20 + " 🧠 思考模式 (Thinking Process) " + "="*20)
            print(thinking_match.group(1).strip())
            print("="*66 + "\n")
        
        if answer_match:
            print("\n" + "="*20 + " 📝 最终回复 (Final Response) " + "="*20)
            print(answer_match.group(1).strip())
        else:
            # 备用：如果只有思考没有 answer 标签，打印剩余部分
            clean_text = re.sub(r'<thinking>.*?</thinking>', '', raw_content, flags=re.DOTALL).strip()
            print("\n" + "="*20 + " 📝 最终回复 (Final Response) " + "="*20)
            print(clean_text)

except Exception as e:
    print(f"发生错误: {e}")
