---
name: excel-test-case-generator
description: Excel用例生成工具 - 根据用户需求生成Excel格式的测试用例文档。当用户需要创建测试用例文档、根据需求生成测试用例或生成功能测试用例表格时使用。支持创建包含用例编号、用例名称、前置条件、测试步骤、预期结果和优先级等标准字段的Excel测试用例文件。
---

# Excel用例生成工具

## 概述

本技能使Claude能够生成Excel格式的测试用例文档。支持根据用户需求、功能规格说明或特性描述创建结构良好的测试用例Excel文件。

## 快速开始

当用户请求生成测试用例时，您应该直接：

1. **分析用户提供的需求/功能描述**
2. **识别测试场景**（正向流程、负向流程、边界值、安全测试）
3. **按照标准结构设计全面的测试用例**
4. **使用生成器脚本或CLI工具生成Excel文件**
5. **将生成的文件提供给用户**

**您不需要用户编写Python代码。** 您应该直接分析需求、设计测试用例并生成Excel文件。

## 直接使用（适用于Claude）

当用户要求生成测试用例时（例如："生成小红书登录测试用例" 或 "为用户注册功能创建测试用例"）：

1. **分析需求**：从用户的描述中提取功能需求
   - 识别要测试的功能/特性
   - 理解预期行为
   - 注意任何约束或特殊条件

2. **识别测试场景**：分解为可测试的场景
   - **正向用例**：正常、预期的用户流程
   - **负向用例**：错误处理、无效输入
   - **边界值**：边界情况、限制、边界条件
   - **安全测试**：SQL注入、XSS、身份验证绕过尝试
   - **业务规则**：验证规则、业务逻辑

3. **设计测试用例**：创建详细的测试用例，包括：
   - 清晰的描述性名称，遵循格式："功能模块-测试场景-具体条件"
   - 详细的步骤说明
   - 具体的预期结果
   - 适当的优先级（P0/P1/P2）

4. **生成Excel**：使用 `scripts/generate_testcases.py` CLI工具或 `test_case_generator.py` 编程方式生成
   ```bash
   # 选项1：使用CLI工具配合JSON文件（推荐用于多个测试用例）
   # 首先创建包含测试用例的JSON文件，然后：
   python scripts/generate_testcases.py --input testcases.json --output test_cases.xlsx
   
   # 选项2：编程方式使用生成器
   from scripts.test_case_generator import TestCaseGenerator
   generator = TestCaseGenerator()
   generator.generate_excel("test_cases.xlsx", test_cases)
   ```

5. **审查**：确保测试用例全面覆盖所有场景

## 测试用例生成工作流

生成测试用例时，请遵循以下工作流：

1. **分析需求**：提取功能需求、用户故事或功能描述
2. **识别测试场景**：将需求分解为可测试的场景（正向、负向、边界情况）
3. **设计测试用例**：按照标准结构创建详细的测试用例
4. **生成Excel**：使用测试用例生成器脚本或CLI工具创建格式化的Excel文件
5. **审查和完善**：确保测试用例全面覆盖所有场景

详细的需求分析指南，请参阅 `references/requirement_analysis.md`。

## 标准测试用例结构

每个测试用例包含以下字段：

- **用例编号 (Test Case ID)**: 自动生成格式 TC-001, TC-002 等
- **用例名称 (Test Case Name)**: 简洁描述要测试的内容
- **测试类型 (Test Type)**: 固定为 "功能测试" (Functional Testing)
- **前置条件 (Preconditions)**: 测试执行前必须满足的条件
- **测试步骤 (Test Steps)**: 详细的逐步测试程序（可多行）
- **预期结果 (Expected Result)**: 每个步骤或整体测试的预期结果
- **优先级 (Priority)**: P0 (高), P1 (中), P2 (低)
- **执行结果 (Execution Result)**: 留空用于测试执行跟踪

## 生成测试用例Excel文件

### 使用CLI工具（推荐）

CLI工具 `scripts/generate_testcases.py` 提供了生成测试用例Excel文件的最简单方法：

**From JSON file:**
```bash
# Create a JSON file with test cases
python scripts/generate_testcases.py --input testcases.json --output test_cases.xlsx
```

**JSON format:**
```json
[
    {
        "name": "用户登录功能-正常登录",
        "preconditions": "系统已启动，用户账号已注册",
        "steps": "1. 打开登录页面\n2. 输入正确的用户名和密码\n3. 点击登录按钮",
        "expected_result": "登录成功，跳转到主页",
        "priority": "P0",
        "test_type": "功能测试"
    }
]
```

**Single test case from command line:**
```bash
python scripts/generate_testcases.py \
    --name "用户登录功能-正常登录" \
    --preconditions "系统已启动，用户账号已注册" \
    --steps "1. 打开登录页面\n2. 输入正确的用户名和密码\n3. 点击登录按钮" \
    --expected "登录成功，跳转到主页" \
    --priority P0 \
    --output test_cases.xlsx
```

### 编程方式使用生成器脚本

对于编程使用，导入并使用 `TestCaseGenerator`：

```python
from scripts.test_case_generator import TestCaseGenerator

generator = TestCaseGenerator()

# Add test cases
test_cases = [
    {
        "name": "用户登录功能-正常登录",
        "preconditions": "系统已启动，用户账号已注册",
        "steps": "1. 打开登录页面\n2. 输入正确的用户名和密码\n3. 点击登录按钮",
        "expected_result": "登录成功，跳转到主页",
        "priority": "P0"
    },
    # ... more test cases
]

# Generate Excel file
generator.generate_excel("test_cases.xlsx", test_cases)
```

### 手动创建Excel

使用 openpyxl 直接创建Excel（参考 xlsx 技能）：

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

wb = Workbook()
sheet = wb.active
sheet.title = "测试用例"

# Define headers
headers = ["用例编号", "用例名称", "测试类型", "前置条件", "测试步骤", "预期结果", "优先级", "执行结果"]

# Set headers with formatting
header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
header_font = Font(bold=True, color="FFFFFF", size=11)

for col_idx, header in enumerate(headers, 1):
    cell = sheet.cell(row=1, column=col_idx)
    cell.value = header
    cell.font = header_font
    cell.fill = header_fill
    cell.alignment = Alignment(horizontal="center", vertical="center")

# Set column widths
column_widths = [12, 30, 12, 25, 40, 30, 10, 12]
for col_idx, width in enumerate(column_widths, 1):
    sheet.column_dimensions[get_column_letter(col_idx)].width = width

# Add test cases
# ... add test case rows with proper formatting

wb.save("test_cases.xlsx")
```

## 测试用例设计原则

### 覆盖率要求

- **正向用例**：测试正常、预期的用户流程
- **负向用例**：测试错误处理、无效输入、边界情况
- **边界值**：测试最小值、最大值和边界条件
- **业务规则**：验证业务逻辑和验证规则

### 测试用例质量指南

1. **清晰性**：每个测试用例应该清晰且无歧义
2. **独立性**：测试用例应该可以独立执行
3. **完整性**：步骤应该足够详细以便执行
4. **可追溯性**：测试用例应该映射到需求/功能
5. **可维护性**：使用一致的命名和结构

### 优先级指南

- **P0 (高)**：关键功能、核心用户流程、安全特性
- **P1 (中)**：重要功能、常见用户场景
- **P2 (低)**：锦上添花的功能、边界情况、小的增强

## 最佳实践

### 测试用例命名

- 使用清晰描述要测试内容的名称
- 格式："功能模块-测试场景-具体条件"
- 示例："用户登录-正常登录-有效账号密码"

### 测试步骤

- 按顺序编号每个步骤
- 具体说明操作（点击、输入、选择等）
- 在相关时包含测试数据
- 保持步骤原子性和可测试性

### 预期结果

- 指定确切的预期结果
- 包含验证点（UI变化、数据更新、消息）
- 明确成功标准

## 高级：测试用例模板

对于复杂场景，考虑为以下情况创建测试用例模板：
- API测试
- UI测试
- 集成测试
- 性能测试

详细的设计模式和示例，请参阅 `references/test_case_design.md`。

## 资源

### scripts/generate_testcases.py
从JSON或命令行参数生成测试用例Excel文件的命令行工具。这是生成测试用例的推荐方式。

### scripts/test_case_generator.py
用于编程方式生成格式化测试用例Excel文件的Python类，具有适当的样式和结构。

### references/test_case_design.md
测试用例设计原则、模式和最佳实践的综合指南。

### references/requirement_analysis.md
分析需求和识别测试场景的指南。包括检查清单、模式和测试用例设计的决策树。
