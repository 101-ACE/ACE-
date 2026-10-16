# Fonts for COSWAY TCM 中醫體質檢測

This directory contains Chinese font files to ensure proper Chinese character rendering in generated reports.

## Font Files
- `NotoSansCJK-Regular.ttc` - Noto Sans CJK Regular (from Google Noto fonts)
- `NotoSansCJK-Black.ttc` - Noto Sans CJK Black (Bold variant)
- `NotoSansTC-Regular.ttf` - Noto Sans TC Regular (Traditional Chinese specific)
- `NotoSansTC-Bold.ttf` - Noto Sans TC Bold (Traditional Chinese specific)

## Font Loading Priority
The `generate_report_image` function loads fonts in this order:
1. Project fonts directory (`backend/fonts/`)
   - Bold variants first for bold text
   - Regular variants for normal text
2. System fonts (if available)
   - Noto Sans CJK fonts from Linux system
   - DejaVu Sans as final fallback
3. PIL default font (last resort - will not display Chinese characters)

## Adding Custom Fonts
To add or update fonts:
1. Place .ttf or .ttc font files in this directory
2. Update the font loading logic in `generate_report_image` if needed
3. Ensure font files are readable by the application

## Testing Font Rendering
Run the following to test Chinese character rendering:
```bash
python3 -c "
from main import generate_report_image
from datetime import datetime

class MockAssessment:
    def __init__(self):
        self.name = '測試用戶'
        self.phone = '852-1234-5678'
        self.referral_code = 'HK241545'
        self.constitution = '氣虛'
        self.health_score = 75
        self.risk_level = '中'
        self.risk_indicators = '[\"睡眠質量差\", \"容易疲倦\", \"免疫力較低\"]'
        self.submitted_at = datetime.now()

assessment = MockAssessment()
img_data = generate_report_image(assessment, ['多喝溫水', '適度運動'])
if img_data:
    with open('/tmp/test_font.png', 'wb') as f:
        f.write(img_data)
    print('✅ Font test image saved to /tmp/test_font.png')
"
```
