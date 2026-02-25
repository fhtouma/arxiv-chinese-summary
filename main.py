import os
import requests
import time
from bs4 import BeautifulSoup
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import markdown

# 导入最新版本的 Gemini SDK
from google import genai

def fetch_daily_papers():
    """抓取arXiv astro-ph.CO 和 astro-ph.GA 类别的最新论文并去重"""
    categories = ["astro-ph.CO", "astro-ph.GA"]
    papers_dict = {}
    
    for category in categories:
        url = f"https://arxiv.org/list/{category}/new"
        print(f"正在抓取分类: {category}...")
        
        try:
            response = requests.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            dt_items = soup.find_all('dt')
            dd_items = soup.find_all('dd')
            
            for dt, dd in zip(dt_items, dd_items):
                arxiv_id = dt.find('a', title="Abstract")['href'].replace('/abs/', '')
                if arxiv_id in papers_dict: continue
                
                title = dd.find('div', class_='list-title').text.replace('Title:', '').strip()
                authors_div = dd.find('div', class_='list-authors')
                authors = [a.text for a in authors_div.find_all('a')]
                abstract = dd.find('p', class_='mathjax').text.strip()
                subjects_div = dd.find('div', class_='list-subjects')
                subjects = subjects_div.text.replace('Subjects:', '').strip() if subjects_div else ""
                
                papers_dict[arxiv_id] = {
                    'arxiv_id': arxiv_id,
                    'title': title,
                    'authors': authors,
                    'abstract': abstract,
                    'subjects': subjects,
                }
        except Exception as e:
            print(f"抓取 {category} 时出错: {e}")
            
    return list(papers_dict.values())

def summarize_single_paper(paper, client, model_id):
    """单独翻译并总结一篇论文（Map 阶段），带有重试机制"""
    
    # 获取第一作者（如果列表不为空）
    first_author = paper['authors'][0] if paper['authors'] else "Unknown"
    
    prompt = f"""你是一位天体物理学专家。请阅读以下 arXiv 论文摘要，并严格按照规定格式输出中文总结。
    
【强制要求】
1. 必须完全遵循下方的【输出模板】，不要增加任何额外的寒暄、标号（如1. 2. 3.）或多余的换行。
2. 每一个条目必须以星号加粗开头，例如：* **核心问题：**
3. 语言简练，专业术语翻译准确。

【论文信息】
ID: {paper['arxiv_id']}
Title: {paper['title']}
Abstract: {paper['abstract']}

【输出模板】（请严格原样输出以下结构）
**[{paper['arxiv_id']}] {paper['title']}**
* **第一作者：** {first_author}
* **核心问题：** [用一句话概括]
* **主要方法：** [使用的数据/仪器/模型]
* **关键结论：** [核心发现或结果]
* **摘要翻译：** [摘要原文的准确完整逐字翻译]
"""
    
    # 工业级做法：添加自动重试机制 (最多重试 3 次)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
            )
            return response.text
        except Exception as e:
            error_msg = str(e).lower()
            # 如果是 429 限流或资源耗尽错误
            if "429" in error_msg or "503" in error_msg or "500" in error_msg or "exhausted" in error_msg or "quota" in error_msg:
                if attempt < max_retries - 1:
                    print(f"    ⚠️ 触发 API 限流 (429)，暂停 60 秒后进行第 {attempt + 2} 次重试...")
                    time.sleep(60) # 强制冷却 1 分钟
                else:
                    return f"**[{paper['arxiv_id']}] {paper['title']}**\n* ❌ 总结失败：API 额度耗尽，请参考原文。\n"
            else:
                # 其他未知错误直接返回
                print(f"    ❌ 论文 {paper['arxiv_id']} 总结发生未知错误: {e}")
                return f"**[{paper['arxiv_id']}] {paper['title']}**\n* ❌ 总结失败：{e}\n"

def generate_overall_summary(all_detailed_summaries, client, model_id):
    """基于所有单篇摘要，生成宏观分类与热点概览（Reduce 阶段）"""
    prompt = f"""你是一位资深天体物理学专家。我将提供今日 arXiv (astro-ph.CO/GA) 所有新论文的简短摘要列表。
请基于这些摘要，帮我生成一份宏观的“今日研究日报”。

要求：
1. **分类整理**：按研究子领域（如早期宇宙、大尺度结构、银河系结构、黑洞等）分类，在每个大类下，用一两句话概述该领域今天的整体进展，并仅挑选(<=10)篇最具代表性的论文 ID 作为例子。
2. **今日研究热点**：在文末总结 3 到 4 个今天最突出的研究趋势或热门话题。
3. **无需罗列所有论文**：不需要把每篇论文都写出来（详细内容在附件中），重点在于高层级的视角和趋势。

以下是今日所有论文的摘要概览：
{all_detailed_summaries}
"""
    try:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"整体宏观总结失败: {e}")
        return "宏观总结生成失败，请查阅附件中的详细摘要。"

def send_email_with_attachment(html_summary, detailed_md, sender, password, recipients, smtp_server):
    """发送包含正文 HTML 和附件 Markdown 文件的邮件"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 根容器：'mixed' 支持正文+附件
    msg = MIMEMultipart('mixed')
    msg['From'] = sender
    msg['To'] = ", ".join(recipients)
    msg['Subject'] = f"🌌 arXiv 天体物理每日速递 (CO & GA) - {today}"
    
    # 1. 邮件正文容器：'alternative' 支持纯文本兜底 + HTML 排版
    body_part = MIMEMultipart('alternative')
    
    text_body = f"arXiv 每日论文总结 - {today}\n（宏观分类概览，详细论文摘要翻译请下载附件查看）\n\n{html_summary}"
    html_content = markdown.markdown(html_summary, extensions=['extra', 'nl2br'])
    
    html_body = f"""
    <html>
      <head>
        <style>
          body {{ font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }}
          h1, h2, h3 {{ color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 8px; margin-top: 24px; }}
          ul, ol {{ padding-left: 20px; }}
          li {{ margin-bottom: 8px; }}
          strong {{ color: #e74c3c; }}
          .footer {{ margin-top: 30px; font-size: 13px; color: #666; background-color: #f9f9f9; padding: 15px; border-radius: 5px; }}
        </style>
      </head>
      <body>
        <h2>🌌 arXiv 每日论文概览 - {today}</h2>
        <div style="background-color: #e8f4f8; padding: 10px 15px; border-left: 4px solid #3498db; margin-bottom: 20px;">
            <strong>📢 提示：</strong>本文仅为宏观分类与趋势提炼。<strong>今日所有抓取到的单篇论文的详细中文翻译（核心问题、方法、结论），已打包在附件的 Markdown 文件中，请下载查阅！</strong>
        </div>
        <div>
          {html_content}
        </div>
        <div class="footer">
          以上内容由 Gemini AI 自动生成。由于 AI 幻觉的存在，内容可能在科学性、翻译准确度上存在瑕疵，请以原文为准。
        </div>
      </body>
    </html>
    """
    
    body_part.attach(MIMEText(text_body, 'plain', 'utf-8'))
    body_part.attach(MIMEText(html_body, 'html', 'utf-8'))
    msg.attach(body_part)
    
    # 2. 邮件附件：添加详细的 Markdown 文件
    filename = f"arXiv_Daily_Detailed_{today}.md"
    attachment = MIMEText(detailed_md, 'plain', 'utf-8')
    attachment.add_header('Content-Disposition', f'attachment; filename="{filename}"')
    msg.attach(attachment)
    
    try:
        with smtplib.SMTP_SSL(smtp_server, 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        print("✅ 邮件及附件发送成功！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipients_str = os.environ.get("RECIPIENT_EMAILS")
    smtp_server = os.environ.get("SMTP_SERVER")

    if not all([api_key, sender_email, sender_password, recipients_str, smtp_server]):
        print("缺少环境变量配置，请检查 GitHub Secrets！")
        return

    recipients = [email.strip() for email in recipients_str.split(',')]
    
    # 获取数据
    papers = fetch_daily_papers()
    if not papers:
        print("今日无新论文。")
        return
        
    print(f"抓取到 {len(papers)} 篇论文，开始逐篇深度总结（预计耗时 {len(papers) * 10 // 60 + 1} 分钟）...")
    
    client = genai.Client(api_key=api_key)
    MODEL_ID = 'gemini-2.5-flash' # 依然推荐使用 Flash 兼顾速度和免费额度
    
    detailed_summaries = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    detailed_md = f"# arXiv astro-ph (CO & GA) 详细论文翻译 - {today_str}\n\n"
    detailed_md += f"共收录 {len(papers)} 篇最新论文。\n\n---\n\n"
    
# 第一阶段：Map - 逐篇处理
    for i, paper in enumerate(papers):
        print(f"[{i+1}/{len(papers)}] 正在处理: {paper['arxiv_id']}")
        
        single_summary = summarize_single_paper(paper, client, MODEL_ID)
        detailed_summaries.append(single_summary)
        
        # 将单篇结果追加到附件文本中
        detailed_md += single_summary + "\n\n---\n\n"
        
        # 防封锁休眠：放慢速度，8秒一次，确保绝对低于 10 RPM 限制
        if i < len(papers) - 1:
            time.sleep(8)
            
    # 第二阶段：Reduce - 宏观汇总
    print("\n所有单篇处理完毕，正在生成宏观趋势概览...")
    # 把所有单篇摘要拼接在一起喂给大模型做大总结
    all_text_for_reduce = "\n".join(detailed_summaries)
    overall_summary = generate_overall_summary(all_text_for_reduce, client, MODEL_ID)
    
    # 第三阶段：发送邮件
    if overall_summary:
        print("宏观总结完毕，准备发送邮件...")
        send_email_with_attachment(overall_summary, detailed_md, sender_email, sender_password, recipients, smtp_server)

if __name__ == "__main__":
    # 为了能在 GitHub Actions 的日志里实时看到打印，记得 YAML 文件里写 python -u main.py
    main()
