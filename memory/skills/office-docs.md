---
name: office-docs
description: Use when user asks to create or edit Word documents, Excel spreadsheets, or PowerPoint presentations
status: active
created: 2026-04-26
last_tested: 2026-04-26
---

# Office Document Generation

## Overview

生成和编辑 Office 文档（Word/Excel/PPT），使用 python-docx、openpyxl、python-pptx，通过 bash 工具执行 Python 脚本。

## Quick Start

```bash
python -c "
from docx import Document
doc = Document()
doc.add_heading('标题', level=1)
doc.save('E:/tmp/output.docx')
print('Done: E:/tmp/output.docx')
"
```

文件路径使用用户指定位置，生成后告知用户路径。

## Word (python-docx)

### 基础文档

```python
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
style = doc.styles['Normal']
style.font.name = '宋体'
style.font.size = Pt(12)

doc.add_heading('文档标题', level=1)
doc.add_paragraph('正文内容')

# 加粗
run = doc.add_paragraph().add_run('加粗文字')
run.bold = True

# 列表
doc.add_paragraph('列表项', style='List Bullet')
doc.add_paragraph('步骤', style='List Number')

doc.save('output.docx')
```

### 表格

```python
table = doc.add_table(rows=3, cols=4, style='Table Grid')
headers = ['姓名', '年龄', '部门', '职位']
for i, h in enumerate(headers):
    cell = table.rows[0].cells[i]
    cell.text = h
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True

data = [['张三', '28', '技术部', '工程师'], ['李四', '32', '市场部', '经理']]
for row_idx, row_data in enumerate(data, 1):
    for col_idx, val in enumerate(row_data):
        table.rows[row_idx].cells[col_idx].text = val
```

### 页面设置

```python
section = doc.sections[0]
section.top_margin = Cm(2.54)
section.bottom_margin = Cm(2.54)
section.left_margin = Cm(3.17)
section.right_margin = Cm(3.17)
```

## Excel (openpyxl)

### 基础工作簿

```python
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

wb = Workbook()
ws = wb.active
ws.title = 'Sheet1'

# 表头
headers = ['姓名', '语文', '数学', '英语', '总分']
for col, h in enumerate(headers, 1):
    cell = ws.cell(row=1, column=col, value=h)
    cell.font = Font(bold=True, size=12)
    cell.alignment = Alignment(horizontal='center')
    cell.fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')

# 数据
data = [['张三', 85, 92, 78], ['李四', 90, 88, 95], ['王五', 76, 95, 82]]
for row_idx, row_data in enumerate(data, 2):
    for col_idx, val in enumerate(row_data, 1):
        ws.cell(row=row_idx, column=col_idx, value=val)

# 公式
for row in range(2, 5):
    ws.cell(row=row, column=5, value=f'=SUM(B{row}:D{row})')

ws.column_dimensions['A'].width = 12
wb.save('output.xlsx')
```

### 多 Sheet + 边框

```python
ws2 = wb.create_sheet('汇总')
ws2.cell(row=1, column=1, value='总人数')

thin_border = Border(left=Side(style='thin'), right=Side(style='thin'),
                     top=Side(style='thin'), bottom=Side(style='thin'))
for row in ws.iter_rows(min_row=1, max_row=4, max_col=5):
    for cell in row:
        cell.border = thin_border
        cell.alignment = Alignment(horizontal='center', vertical='center')
```

## PPT (python-pptx)

### 基础演示文稿

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)

# 标题页
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = '演示标题'
slide.placeholders[1].text = '副标题'

# 内容页
slide2 = prs.slides.add_slide(prs.slide_layouts[1])
slide2.shapes.title.text = '章节标题'
body = slide2.placeholders[1]
tf = body.text_frame
tf.text = '第一点'
tf.add_paragraph().text = '第二点'

# 空白页 + 自定义文本
slide3 = prs.slides.add_slide(prs.slide_layouts[6])
txBox = slide3.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
tf = txBox.text_frame
tf.text = '自定义文本'
for para in tf.paragraphs:
    for run in para.runs:
        run.font.size = Pt(28)

prs.save('output.pptx')
```

### 表格页

```python
slide = prs.slides.add_slide(prs.slide_layouts[6])
table_shape = slide.shapes.add_table(4, 3, Inches(1), Inches(1.5), Inches(8), Inches(3))
table = table_shape.table
for i, h in enumerate(['项目', '进度', '负责人']):
    table.cell(0, i).text = h
for r, row in enumerate([['项目A', '80%', '张三'], ['项目B', '60%', '李四']], 1):
    for c, val in enumerate(row):
        table.cell(r, c).text = val
```

## Common Mistakes

| 问题 | 解决方案 |
|------|---------|
| 中文乱码 | 设置 `style.font.name = '宋体'` |
| 表格无边框 | 使用 `style='Table Grid'` |
| 列宽不合适 | 手动设置 `ws.column_dimensions['A'].width = 12` |

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
