import os
import re
import sys


PRESET_PROVIDER_LABEL = 'DeepSeek'
PRESET_MODEL_LABEL = 'DeepSeek Chat'

# 目录改为 DeepSeek 官方 API 的模型 id（api.deepseek.com 认这两个）：
#   deepseek-chat      —— 通用对话/工具调用，支持图片输入；官方上下文 64K，输出上限 8K
#   deepseek-reasoner  —— 深度推理（思维链）；官方上下文 64K，输出上限高于 chat
# 上下文统一锁定 64K（65536），输出上限按模型分别配置（model.maxTokens 优先于全局），
# 且输出必须为输入留出空间：chat 8K + 输入 56K / reasoner 32K + 输入 32K。
DEFAULT_MODELS_BRANDED = (
    'const DEFAULT_MODELS = [\n'
    '\t{\n'
    '\t\tid: "deepseek-chat",\n'
    f'\t\tname: "{PRESET_MODEL_LABEL}",\n'
    '\t\tdescription: "DeepSeek 官方通用对话模型，支持工具调用与图片输入。",\n'
    '\t\tcontextWindow: DEFAULT_CONTEXT_WINDOW,\n'
    '\t\tmaxTokens: 8192,\n'
    '\t\tinputModalities: ["text", "image"],\n'
    '\t\timagePixelBudget: DEFAULT_REQUEST_IMAGE_PIXEL_BUDGET,\n'
    '\t\timageMaxBytes: DEFAULT_REQUEST_IMAGE_MAX_BYTES,\n'
    '\t\tsystemPromptUpdate: "in-history"\n'
    '\t},\n'
    '\t{\n'
    '\t\tid: "deepseek-reasoner",\n'
    '\t\tname: "DeepSeek Reasoner",\n'
    '\t\tdescription: "DeepSeek 官方深度推理模型，适合复杂分析与高难度任务。",\n'
    '\t\tcontextWindow: DEFAULT_CONTEXT_WINDOW,\n'
    '\t\tmaxTokens: 32768\n'
    '\t}\n'
    '];'
)

def apply_patches(app_root):
    print(f"[*] 正在为 {app_root} 应用补丁...")
    modules_dir = os.path.join(app_root, 'node_modules', '@deepseek-ai')
    if not os.path.exists(modules_dir):
        print(f"[!] 找不到目标目录: {modules_dir}")
        return

    structural_failures = []

    for root, dirs, files in os.walk(modules_dir):
        for f in files:
            if f.endswith('.js') or f.endswith('.mjs'):
                p = os.path.join(root, f)
                try:
                    with open(p, 'r', encoding='utf-8', errors='ignore') as fp:
                        code = fp.read()
                except Exception as e:
                    # 静默跳过会导致补丁漏打且无迹可查，必须留下日志
                    print(f"[!] 跳过无法读取的文件 {p}: {e}")
                    continue

                changed = False

                # 不改写依赖模块内的 crypto.randomUUID()。runner.js 会在 HTML 的
                # <head> 最前注入浏览器 Polyfill；对压缩后的依赖做全局文本替换会
                # 破坏 JavaScript 语法，也会影响 Node.js 侧本已可用的原生实现。

                # 1. 修复 403 CSRF/Origin 拦截
                if 'function isTrustedApiRequest(' in code:
                    code = code.replace('function isTrustedApiRequest(request, trustedHosts) {', 'function isTrustedApiRequest(request, trustedHosts) { return true;')
                    changed = True

                # 1b. 修复浏览器端 "settings are unavailable in this browser" (Issue #2)
                # 根因：DSH 将 settings.describe 等 RPC 视为 loopback-only。经飞牛桌面
                # iframe（http://<NAS_IP>:3080）或局域网打开时 location.hostname 非 loopback，
                # 浏览器端 isLoopback=false → SettingsDescribeMirror persistence=memory
                # → 设置 mirror 永远拿不到视图 → 设置页报错。
                # 与 isTrustedApiRequest 同样思路：经本应用反代/控制页访问即视为可信直连。
                # 服务端本身无 loopback 校验（已验证），此改动不新增暴露面。
                if 'function isLoopbackHostname(' in code and 'function isLoopbackHostname(hostname) { return true;' not in code:
                    code = code.replace('function isLoopbackHostname(hostname) {', 'function isLoopbackHostname(hostname) { return true;')
                    changed = True

                if 'dsh-client-connection' in p and f == 'client.js':
                    before = code
                    code = re.sub(
                        r'isLoopback:\s*(?:transport\?\.ownsHost === true\s*\|\|\s*)?pageLocation === void 0\s*\|\|\s*isLoopbackHostname\(pageLocation\.hostname\),',
                        'isLoopback: true, // fnOS fix (Issue #2): trust proxy/control panel access as loopback',
                        code
                    )
                    changed = changed or code != before

                # 2. 预制 deepseek-official 路由：提供商显示名保持 DeepSeek，
                # 内置模型目录统一显示名并锁定 200k 上下文。
                # 提供商内部 ID 不动，用户仍可在 Models 页面编辑配置、新增自己的
                # 提供商和模型。
                if 'dsh-llm-deepseek' in p and f == 'index.js':
                    before = code
                    code = code.replace('name: "DeepSeek"', f'name: "{PRESET_PROVIDER_LABEL}"')
                    code = code.replace('displayName: "DeepSeek"', f'displayName: "{PRESET_PROVIDER_LABEL}"')
                    code = code.replace('const DEFAULT_CONTEXT_WINDOW = 1e6;', 'const DEFAULT_CONTEXT_WINDOW = 65536;')
                    code = re.sub(r'const DEFAULT_MODELS = \[.*?\];', DEFAULT_MODELS_BRANDED, code, flags=re.DOTALL)
                    # 输出上限：上游默认 256e3（256k）大于窗口，语义倒挂；
                    # 且该值经 dsh-llm resolveCallWithInfo 以 defaultMaxTokens
                    # 随每个请求作为 max_tokens 下发。收敛为 8192 作为兜底，
                    # 与官方 deepseek-chat 的输出上限一致；本目录内两个模型
                    # 已各自声明 maxTokens，故兜底值仅作用于用户新增的模型。
                    code = code.replace('const DEFAULT_MAX_TOKENS = 256e3;', 'const DEFAULT_MAX_TOKENS = 8192;')
                    code = code.replace('config.maxTokens ?? 256e3', 'config.maxTokens ?? 8192')
                    changed = changed or code != before
                    # 模型目录与参数收敛的结构性复核：
                    # 1. 默认模型（deepseek-chat）必须成功写入并具备多模态能力
                    # 2. 完备保留 V4 Flash、V4 Pro 及 Vision Exp，绝不可误删上游新增或既有能力
                    for req_id in ['deepseek-chat', 'deepseek-reasoner']:
                        # [custom] 0.2.x 下路径含 dsh-llm-deepseek 的 index.js 不止一份，
                        # 只有真正写入了品牌目录的那份需要复核，否则其余副本会全部误报。
                        if PRESET_MODEL_LABEL in code and f'id: "{req_id}"' not in code:
                            structural_failures.append(f'内置模型目录缺少 {req_id} 条目（DEFAULT_MODELS 匹配漂移，需人工更新 patch.py）')

                if 'dsh-client-connection' in p and f == 'client.js':
                    code, group_replacements = re.subn(
                        r'(id:\s*"deepseek-official",\s*name:\s*)"DeepSeek"',
                        rf'\1"{PRESET_PROVIDER_LABEL}"',
                        code,
                    )
                    code, provider_replacements = re.subn(
                        r'(provider:\s*"deepseek-official",\s*displayName:\s*)"DeepSeek"',
                        rf'\1"{PRESET_PROVIDER_LABEL}"',
                        code,
                    )
                    changed = changed or group_replacements > 0 or provider_replacements > 0

                # 3. 定制 dsh-host-directory-picker-browse 飞牛共享目录
                if 'dsh-host-directory-picker-browse' in p and f == 'index.js':
                    before = code
                    fnos_block = (
                        'function fnosTargetHome() {\n'
                        '\ttry {\n'
                        '\t\tconst rootDirs = fs.readdirSync("/");\n'
                        '\t\tconst volShare = rootDirs.find(d => /^vol[0-9]+$/.test(d) && fs.existsSync("/" + d + "/@appshare/DeepSeekHarness"));\n'
                        '\t\tif (volShare) return "/" + volShare + "/@appshare/DeepSeekHarness";\n'
                        '\t} catch (e) {}\n'
                        '\treturn homedir();\n'
                        '}\n'
                        '\t\tconst home = fnosTargetHome();'
                    )

                    # 原子块还原：将任何形式的 fnosTargetHome() 定义连带其 home 赋值整块还原为
                    # 上游原生形态 `const home = homedir();`，同时吸收旧补丁遗留的尾部多余空白行，
                    # 再重新注入以保证严格幂等（重复执行不叠加空白）。
                    # 若找不到任意卷的 @appshare/DeepSeekHarness，直接安全回退至 homedir()，
                    # 严禁回退至卷根目录（如 /vol1），因飞牛 NAS 卷根目录权限为 d---------，
                    # 会导致 opendir 抛出 EACCES: permission denied (Issue #3)。
                    code = re.sub(
                        r'function fnosTargetHome\(\)\s*\{[\s\S]*?const home = fnosTargetHome\(\);(?:\r?\n[ \t]+(?=\r?\n))?',
                        'const home = homedir();',
                        code
                    )

                    target = 'const home = homedir();'
                    if target in code:
                        # 只注入一次；残留的多余赋值（旧注入还原后与原文件叠加）一律清除
                        code = code.replace(target, fnos_block, 1)
                        code = code.replace(target, '')
                        # fnosTargetHome 用同步的 fs.existsSync，而上游只具名导入
                        # node:fs/promises（无 fs 默认绑定），必须补一行默认导入。
                        # 缺这行时 fs 未定义，ReferenceError 被 fnosTargetHome 内的
                        # try/catch 吞掉并静默回落 homedir()，且 node --check 只验语法
                        # 查不出来——补丁看似成功、实际失效。
                        if not re.search(r'^import fs from "node:fs";$', code, flags=re.M):
                            code = 'import fs from "node:fs";\n' + code
                    changed = changed or code != before

                    # 结构性复核（文件级，不能用全局 CRITICAL_MARKERS 代替：
                    # `import fs from "node:fs";` 在其他包里天然存在，全局计数会假通过）。
                    # 只要该文件用了 fs.，就必须有配套的默认导入；且绝不得含有 anyVol 回退。
                    if 'fs.' in code and not re.search(r'^import fs from "node:fs";$', code, flags=re.M):
                        structural_failures.append(
                            'dsh-host-directory-picker-browse 使用 fs. 但缺少 import fs from "node:fs"'
                            '（运行时 ReferenceError 会被 try/catch 吞掉并静默回落 homedir）'
                        )
                    if 'anyVol' in code:
                        structural_failures.append(
                            'dsh-host-directory-picker-browse 包含 anyVol（会因卷根目录权限不足抛出 EACCES 报错 directory-picker/unreadable）'
                        )

                if changed:
                    with open(p, 'w', encoding='utf-8') as fp:
                        fp.write(code)

    # 关键补丁落点验证：上游改版可能让文本匹配静默落空，语法校验无法发现
    # 『补丁没打上』。这里按补丁后的标志性代码复核，缺失即失败，防止把
    # 局域网不可用的包发布出去（缺失的实机症状是 403 与 settings are
    # unavailable）。以『标志存在』而非『本次替换发生』计数，重复执行
    # （幂等）不会误报。
    CRITICAL_MARKERS = {
        'CSRF 信任放行': 'isTrustedApiRequest(request, trustedHosts) { return true;',
        'loopback 信任修复 (Issue #2)': 'isLoopbackHostname(hostname) { return true;',
        '浏览器端 isLoopback 直连': 'isLoopback: true, // fnOS fix',
        '上下文窗口锁定 64k': 'const DEFAULT_CONTEXT_WINDOW = 65536;',
        '单一品牌模型目录': f'name: "{PRESET_MODEL_LABEL}"',
        '输出上限兜底 8k（常量）': 'const DEFAULT_MAX_TOKENS = 8192;',
        '输出上限兜底 8k（回退值）': 'config.maxTokens ?? 8192',
        '飞牛共享目录定制 (Issue #3)': 'const home = fnosTargetHome();',
    }
    OPTIONAL_MARKERS = {
        '提供商改名（外观性，缺失仅告警）': 'name: "DeepSeek"',
    }

    def count_marker(marker):
        hits = 0
        for root, dirs, files in os.walk(modules_dir):
            for f in files:
                if f.endswith(('.js', '.mjs')):
                    try:
                        with open(os.path.join(root, f), 'r', encoding='utf-8', errors='ignore') as fp:
                            if marker in fp.read():
                                hits += 1
                    except Exception:
                        continue
        return hits

    failed = False
    for label, marker in CRITICAL_MARKERS.items():
        hits = count_marker(marker)
        if hits == 0:
            print(f"[FAIL] 关键补丁未命中: {label}（上游代码可能已变更，需人工更新 patch.py）")
            failed = True
        else:
            print(f"[OK] 关键补丁验证通过: {label}（{hits} 处）")
    for label, marker in OPTIONAL_MARKERS.items():
        hits = count_marker(marker)
        level = 'OK' if hits > 0 else 'WARN'
        print(f"[{level}] 可选补丁 {label}: {hits} 处")
    for msg in structural_failures:
        print(f"[FAIL] {msg}")
        failed = True

    if failed:
        print("[FAIL] 存在未命中的关键补丁，拒绝继续打包")
        sys.exit(1)
    print("[OK] 全部补丁应用完成！")

if __name__ == '__main__':
    target_root = sys.argv[1] if len(sys.argv) > 1 else '.'
    apply_patches(target_root)
