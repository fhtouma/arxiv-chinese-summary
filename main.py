import os
import requests
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

def summarize_with_gemini(papers, api_key):
    """调用最新的 Gemini 模型进行总结"""
    # 初始化新版客户端
    client = genai.Client(api_key=api_key)
    
    # 请根据你在 Google AI Studio 中查到的可用模型名称进行替换
    # 如果可用列表里有 gemini-2.5-pro，就用它；如果有 gemini-1.5-pro 也可以换回去
    MODEL_ID = 'gemini-2.5-flash'
    
    prompt = """你是一位顶尖的天体物理学专家。请帮我总结以下来自arXiv (astro-ph.CO/GA) 的最新论文：
1. 按研究子领域分类整理。
2. 对每篇论文用中文简炼总结：核心问题、主要方法、关键结论。
3. 结尾提供今日研究热点概述。
以下是今日论文信息：\n"""
    
    for paper in papers:
        prompt += f"\nID: {paper['arxiv_id']} | 标题: {paper['title']}\n"
        prompt += f"作者: {', '.join(paper['authors'][:3])} 等\n摘要: {paper['abstract']}\n"
    
    try:
        print(f"正在调用 {MODEL_ID} 模型进行分析总结...")
        # 新版 SDK 的调用方法
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"Gemini API调用失败: {e}")
        return None

def send_email(summary, sender, password, recipients, smtp_server):
    """发送包含漂亮排版的 HTML 邮件"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 使用 'alternative' 允许同时附带纯文本和 HTML
    msg = MIMEMultipart('alternative')
    msg['From'] = sender
    msg['To'] = ", ".join(recipients)
    msg['Subject'] = f"🌌 arXiv 天体物理每日速递 (CO & GA) - {today}"
    
    # 1. 纯文本版本（作为兜底后备）
    text_body = f"arXiv 每日论文总结 - {today}\n{'='*40}\n\n{summary}\n\n--\n由 Gemini AI 自动生成。"
    
    # 2. HTML 版本
    # 将 Gemini 生成的 markdown 转化为 HTML，支持表格和换行等扩展语法
    html_summary = markdown.markdown(summary, extensions=['extra', 'nl2br'])
    
    # 加上一点基础的 CSS 样式
    html_body = f"""
    <html>
      <head>
        <style>
          body {{ font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }}
          h1, h2, h3 {{ color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 8px; margin-top: 24px; }}
          ul, ol {{ padding-left: 20px; }}
          li {{ margin-bottom: 8px; }}
          strong {{ color: #e74c3c; }}
          .footer {{ margin-top: 30px; font-size: 12px; color: #888; border-top: 1px solid #eee; padding-top: 10px; }}
        </style>
      </head>
      <body>
        <h2>🌌 arXiv 每日论文总结 - {today}</h2>
        <div>
          {html_summary}
        </div>
        <div class="footer">
          <p>以上内容由 Gemini AI 总结每日 arXiv 最新论文摘要产生。由于 AI 幻觉的存在，内容可能在科学性、翻译准确度上存在瑕疵，请以原文为准！</p>
        </div>
      </body>
    </html>
    """
    
    # 将两部分内容挂载到邮件体中
    part1 = MIMEText(text_body, 'plain', 'utf-8')
    part2 = MIMEText(html_body, 'html', 'utf-8')
    
    msg.attach(part1)
    msg.attach(part2)
    
    try:
        with smtplib.SMTP_SSL(smtp_server, 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        print("邮件发送成功！")
    except Exception as e:
        print(f"邮件发送失败: {e}")

def main():
    # 从环境变量获取配置 (由 GitHub Secrets 提供)
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
    if papers:
        print(f"抓取到 {len(papers)} 篇论文，开始总结...")
        summary = summarize_with_gemini(papers, api_key)
        if summary:
            print("总结生成完毕，准备发送邮件...")
            send_email(summary, sender_email, sender_password, recipients, smtp_server)
    else:
        print("今日无新论文。")

if __name__ == "__main__":
    main()
