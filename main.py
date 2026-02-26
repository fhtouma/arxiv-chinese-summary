import os
import requests
import time
from bs4 import BeautifulSoup
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import markdown

from google import genai

def fetch_daily_papers():
    """抓取arXiv astro-ph.GA 和 astro-ph.CO 类别的最新论文并去重，仅限 New submissions"""
    categories = ["astro-ph.GA", "astro-ph.CO"]
    papers_dict = {}
    
    for category in categories:
        url = f"https://arxiv.org/list/{category}/new"
        print(f"正在抓取分类: {category}...")
        
        try:
            response = requests.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 直接无视嵌套，提取网页上所有的 dt(论文ID) 和 dd(论文详情)
            dt_items = soup.find_all('dt')
            dd_items = soup.find_all('dd')
            
            new_count = 0
            for dt, dd in zip(dt_items, dd_items):
                # 【终极防漏杀招】：向上寻找最近的标题标签
                # 这样可以精准判断当前这篇论文属于 New, Cross-lists 还是 Replacements
                section_header = dt.find_previous(['h1', 'h2', 'h3', 'h4', 'h5'])
                
                # 如果它头顶上最近的标题不是 "new submissions"，说明它是跨分类或旧版更新，直接跳过！
                if not section_header or 'new submissions' not in section_header.text.lower():
                    continue
                
                # 此时 100% 确定这是纯新提交的论文！
                new_count += 1
                
                # 安全提取 arXiv ID
                a_tag = dt.find('a', title="Abstract")
                if not a_tag:
                    continue
                arxiv_id = a_tag['href'].replace('/abs/', '')
                
                # 全局去重：如果 GA 和 CO 抓到了同一篇，只保留一次
                if arxiv_id in papers_dict: 
                    continue
                
                # 提取标题
                title_div = dd.find('div', class_='list-title')
                title = title_div.text.replace('Title:', '').strip() if title_div else "No Title"
                
                # 提取作者
                authors_div = dd.find('div', class_='list-authors')
                authors = [a.text for a in authors_div.find_all('a')] if authors_div else []
                
                # 提取摘要
                mathjax_p = dd.find('p', class_='mathjax')
                abstract = mathjax_p.text.strip() if mathjax_p else "No Abstract"
                
                # 提取主题
                subjects_div = dd.find('div', class_='list-subjects')
                subjects = subjects_div.text.replace('Subjects:', '').strip() if subjects_div else ""
                
                papers_dict[arxiv_id] = {
                    'arxiv_id': arxiv_id,
                    'title': title,
                    'authors': authors,
                    'abstract': abstract,
                    'subjects': subjects,
                }
            print(f"  -> ✅ 成功定位！在 {category} 中筛选出 {new_count} 篇纯新提交论文。")
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
    """生成宏观分类与热点概览，同样添加重试机制"""
    prompt = f"""你是一位资深天体物理学专家。我将提供今日 arXiv (astro-ph.CO/GA) 所有纯新提交论文的简短摘要列表。
请基于这些摘要，帮我生成一份宏观的“今日研究日报”。

要求：
1. **分类整理**：按研究子领域（如早期宇宙、大尺度结构、银河系结构、黑洞等）分类，在每个大类下，用一两句话概述该领域今天的整体进展，并仅挑选 <=5 篇最具代表性的论文 ID 作为例子。使用一致的列表格式。
2. **今日研究热点**：在文末总结 3 到 4 个今天最突出的研究趋势或热门话题。
3. **无需罗列所有论文**：重点在于高层级的视角和趋势。

以下是今日所有论文的摘要概览：
{all_detailed_summaries}
"""
    max_retries = 10
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
            )
            return response.text
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "503" in error_msg or "500" in error_msg or "exhausted" in error_msg or "quota" in error_msg:
                if attempt < max_retries - 1:
                    print(f"⚠️ 宏观总结 API 限流或拥堵，暂停 60 秒后重试 ({attempt + 1}/{max_retries})...")
                    time.sleep(60)
                else:
                    return "❌ 宏观总结生成失败：API 额度耗尽或持续拥堵。请查阅附件中的详细摘要。"
            else:
                return f"❌ 宏观总结生成失败：{e}。请查阅附件中的详细摘要。"

def send_email_with_attachment(html_summary, detailed_md, sender, password, recipients, smtp_server):
    """发送包含正文 HTML 和附件 Markdown 文件的邮件"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    msg = MIMEMultipart('mixed')
    msg['From'] = sender
    msg['To'] = ", ".join(recipients)
    msg['Subject'] = f"🌌 arXiv 天体物理每日速递 (CO & GA) - {today}"
    
    body_part = MIMEMultipart('alternative')
    text_body = f"arXiv 每日论文总结 - {today}\n\n{html_summary}"
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
            <strong>📢 提示：</strong>本文仅为宏观分类与趋势提炼。<strong>今日所有抓取到的单篇纯新论文（New Submissions）的详细中文翻译，已打包在附件的 Markdown 文件中，请下载查阅！</strong>
        </div>
        <div>
          {html_content}
        </div>
        <div class="footer">
          以上内容由 Gemini AI 自动生成。请以原文为准。
        </div>
      </body>
    </html>
    """
    
    body_part.attach(MIMEText(text_body, 'plain', 'utf-8'))
    body_part.attach(MIMEText(html_body, 'html', 'utf-8'))
    msg.attach(body_part)
    
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
    
    papers = fetch_daily_papers()
    if not papers:
        print("今日无纯新论文提交。")
        return
        
    print(f"成功筛选出 {len(papers)} 篇纯新提交论文 (New submissions)！")
    print(f"开始逐篇深度总结，基础间隔设为 60 秒，预计耗时 {len(papers)} 分钟...")
    
    client = genai.Client(api_key=api_key)
    MODEL_ID = 'gemini-3-flash-preview'
    
    detailed_summaries = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    detailed_md = f"# arXiv astro-ph (CO & GA) 详细论文翻译 - {today_str}\n\n"
    detailed_md += f"共收录 {len(papers)} 篇今日纯新提交 (New submissions) 论文。\n\n---\n\n"
    
    for i, paper in enumerate(papers):
        print(f"[{i+1}/{len(papers)}] 正在处理: {paper['arxiv_id']}")
        
        single_summary = summarize_single_paper(paper, client, MODEL_ID)
        detailed_summaries.append(single_summary)
        
        detailed_md += single_summary + "\n\n---\n\n"
        
        # 【升级】所有正常请求之间也强制休眠 60 秒，彻底确保安全！
        if i < len(papers) - 1:
            print("    ⏳ 等待 60 秒以防触发 API 速率限制...")
            time.sleep(60) 
            
    print("\n所有单篇处理完毕，正在生成宏观趋势概览...")
    all_text_for_reduce = "\n".join(detailed_summaries)
    overall_summary = generate_overall_summary(all_text_for_reduce, client, MODEL_ID)
    
    if overall_summary:
        print("宏观总结完毕，准备发送邮件...")
        send_email_with_attachment(overall_summary, detailed_md, sender_email, sender_password, recipients, smtp_server)

if __name__ == "__main__":
    main()
