"""前端静态文件的结构性检查。

不是像素级测试，而是把**真实出过的 bug** 变成断言。目前守两条：

1. `hidden` 属性必须真的隐得住。
   `hidden` 只是 UA 样式表里的 `display: none`，作者样式里写的任何 display
   （`.gate` / `.app` 都是 flex）都会把它盖掉 → 「已隐藏」的登录页/启动页
   仍按 `min-height: 100%` 占满一屏并画上不透明底色，把面板整体顶下去，
   表现就是**页面顶部一大片空白**（手机上尤其明显）。
2. 靠 `hidden` 切换的骨架元素必须齐全，且不许用内联 style 代替。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "cpapanel", "web", "static")


def _read(name):
    with open(os.path.join(STATIC, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _strip_css_comments(css):
    """拔掉注释再匹配。

    踩过：样式表里的注释本身会写选择器样例（如 `.gate { min-height: 100% }`），
    正则会在注释里匹到假规则，于是断言莫名其妙地失败。
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


class HiddenAttributeTest(unittest.TestCase):
    def setUp(self):
        self.html = _read("index.html")
        self.css = _read("style.css")
        self.css_live = _strip_css_comments(self.css)

    def test_global_hidden_rule_beats_author_display(self):
        """必须有一条**全局** `[hidden] { display: none !important }`。

        逐个元素写 `.xxx[hidden]` 的做法曾经漏掉 `#gate` 和 `#booting`，
        所以这里断言的是「全局兜底在不在」，而不是「每个元素都单独写了」。
        """
        match = re.search(r"\[hidden\]\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, "style.css 里找不到 [hidden] 规则")
        body = match.group(1)
        self.assertRegex(body, r"display:\s*none")
        self.assertIn("!important", body,
                      "缺 !important 就会依赖源码先后顺序，容易被之后新增的 display 规则盖掉")

    def test_hidden_elements_do_not_use_inline_style(self):
        """用 hidden 属性切换的元素不该改用内联 style —— 那种写法这条规则就管不到了。"""
        elements = re.findall(r"<[^>]*\shidden[^>]*>", self.html)
        self.assertTrue(elements, "index.html 里应当至少有一个初始 hidden 的元素")
        for tag in elements:
            self.assertNotIn("style=", tag, f"不要用内联 style 代替 hidden：{tag[:90]}")

    def test_skeleton_gates_are_hidden_initially(self):
        """骨架三件套齐全，且切换关系正确。

        `#gate`（登录页）与 `#app`（应用外壳）初始必须 hidden；
        `#booting`（加载中）相反 —— 它一开始就该看得见，否则白屏。
        """
        for element_id, expect_hidden in (("booting", False), ("gate", True), ("app", True)):
            tag = re.search(r'<[^>]*id="%s"[^>]*>' % element_id, self.html)
            self.assertIsNotNone(tag, f"index.html 里找不到 #{element_id}")
            has_hidden = "hidden" in tag.group(0)
            self.assertEqual(has_hidden, expect_hidden,
                             f"#{element_id} 初始 hidden 应为 {expect_hidden}")

    def test_layout_classes_have_a_display_rule(self):
        """`.gate` / `.app` 确实是 flex —— 正是它们会盖掉 UA 的 display:none。

        这条是为了留住「为什么需要那条全局规则」的证据：如果哪天它们不再设置
        display，上面的风险也就不存在了（那时可以放心删掉这里的断言）。
        """
        for selector in (r"\.gate\s*\{", r"\.app\s*\{"):
            block = re.search(selector + r"([^}]*)\}", self.css_live)
            self.assertIsNotNone(block, f"style.css 里找不到 {selector}")
            self.assertIn("display:", block.group(1),
                          "这两个容器设置了 display，会覆盖 hidden 的 UA 行为")


if __name__ == "__main__":
    unittest.main()
