# 装载基准漂移修复 — 交付说明

## 一、问题现象

数仓间交换的规范文本（XSD）以**模式文本**形态随数据域发布到各装载节点。同一份含相对引用
（`xs:include` / `xs:import` / `xs:redefine` / `xs:override` 的 `schemaLocation`，以及实例文档的
`xsi:schemaLocation`）的规范文本：

- 在「相对件所在目录」启动进程时装载成功；
- 在其它目录启动进程时装载失败；
- 入仓校验链路因此**间歇性误拒**，跨节点行为不可复现。

## 二、根因链

1. 规范以**内存文本源**（XML 字符串 / bytes / `StringIO` / `BytesIO` / Element）装载时，
   `XMLResource` 没有属于自身的 URL（`self.url is None`），因此 `BaseUrlOption` 描述符
   （`xmlschema/arguments.py`）无法从源 URL 推导出基准目录，`base_url` 为 `None`。
2. 装载相对引用时，链路最终调用 `xmlschema/utils/urls.py::normalize_url(location, base_url)`。
3. 当 `location` 是相对本地路径且 `base_url is None` 时，`normalize_url()` 的**基准兜底实现**
   直接以进程工作目录作为基准：

   ```python
   base_path = LocationPath(os.getcwd())
   return base_path.joinpath(path).normalize().as_uri()
   ```

4. 于是相对件被解析到 `os.getcwd()` 之下。该基准随进程启动目录变化，导致同一份文本在不同
   节点（或同一节点不同工作目录）解析出不同的绝对 URL —— 即**装载基准漂移到进程工作目录**。
5. 相对件恰好在工作目录下时命中成功，否则 `urlopen` 失败，include/import 被记入
   `missing_locations` 并告警/报错，表现为跨节点不可复现的间歇性误拒。

对比：**文件路径源**（或 URL 源）的 `XMLResource.url` 非空，`BaseUrlOption` 始终从源 URL 取
其所在目录作为基准，与工作目录无关，因此文件源装载稳定、可复现。漂移只发生在无自身 URL 的
内存源上。

## 三、修复方案与基准语义决策

### 3.1 内存文本源：采用「显式基准」语义（自行设计决策）

为内存源选择**显式基准**而非兼容兜底，理由：

- 规范随数据域发布到多节点，**可复现性是入仓校验正确性的前提**；以进程工作目录为隐式基准
  本质上把装载结果绑定到部署环境，无法在不同节点复现，正是本次故障根因。
- 内存文本在语义上没有「所在目录」，不存在天然、确定的基准；任何隐式兜底（包括工作目录）
  都是人为选择，且工作目录是其中最不稳定、最不可预期的一个。
- 显式基准把「相对件相对于何处」变成调用方必须明确声明的契约，失败方式从「静默漂移后偶然
  找不到文件」变为「尽早、明确、可定位的报错」，更符合数据装载链路的 fail-fast 要求。

具体规则（新函数 `xmlschema/utils/urls.py::resolve_relative_url`）：

- 相对**本地**引用 + 无显式 `base_url` → 抛 `XMLSchemaBaseURLError`，错误信息**明确指向缺失的
  相对件名**，并提示通过 `base_url` 传入包含该相对件的目录；
- 绝对 URL、绝对本地路径、UNC/盘符路径、远程引用 → 不需要基准，行为与 `normalize_url` 完全
  一致；
- 显式传入 `base_url` → 委托 `normalize_url`，解析结果与既有实现逐字一致；
- `keep_relative=True` → 保留相对形式，不报错（既有逃生口保留）。

### 3.2 为什么不选「兼容兜底」

任务允许的另一选项是「漂移可编程读取 + 弃用提示」。未采用，因为：

- 只要内存源仍可静默回退到工作目录，跨节点不可复现的根因就仍然存在，无法真正消除间歇性
  误拒；
- 显式基准的迁移成本低（调用方补一个 `base_url` 即可），且新报错信息直接给出修复指引。

### 3.3 文件路径源与显式基准：既有语义逐字不回退

- `normalize_url()` 本身**一行未改**，其工作目录兜底原样保留，因此文件路径源、直接调用
  `normalize_url`/`normalize_locations` 的既有行为逐字不变。
- 显式传入 `base_url` 的既有行为逐字不变（新函数直接委托 `normalize_url`）。
- 判别「是否拥有自身基准」用新增的 `XMLResource.has_own_url`（`self.url is not None or
  is_url(self._source)`）。特别处理了构造期 `self.url` 尚未赋值的时序：此时通过检查 `_source`
  自身是否为路径/URL 来判别，保证文件路径源在 `__init__` 内的首次归一化也走旧路径，不会被
  误判为内存源。

### 3.4 import 的命名空间兜底不被短路

`xs:import` 是按**命名空间**引入的。旧行为下，相对 `schemaLocation` 解析失败后，命名空间仍
可能由已注册的 `LOCATIONS` / `FALLBACK_LOCATIONS`（如包内置的 `xmldsig-core-schema.xsd`）满足。

修复后，内存源缺基准时 import 不再在归一化阶段直接抛出，而是：

1. 保留原始相对 location 与基准错误，继续走 `import_namespace` 的位置候选与 fallback 流程；
2. 若命名空间由 fallback/注册位置成功满足，则正常装载（保持旧能力）；
3. 仅当**没有任何候选位置**可满足该命名空间时，才把指向缺失相对件的 `XMLSchemaBaseURLError`
   作为明确错误上报（strict 抛 `XMLSchemaParseError`，lax 收集为错误，skip 忽略）。

`xs:include` / `xs:redefine` / `xs:override` 没有命名空间兜底语义，缺基准时直接给明确报错并
指向缺失相对件。

## 四、改动清单

| 文件 | 改动 |
| --- | --- |
| `xmlschema/exceptions.py` | 新增 `XMLSchemaBaseURLError`（继承 `XMLSchemaValueError`，即 `ValueError`） |
| `xmlschema/utils/urls.py` | 新增 `resolve_relative_url()`；`normalize_url()` 未改动 |
| `xmlschema/resources/xml_resource.py` | 新增 `has_own_url` 属性；`get_url()` 与 `get_locations()` 按源形态分流 |
| `xmlschema/loaders.py` | 新增 `resolve_schema_location()`；include/import 与两个 loader 子类按源形态处理；import 保留 fallback 后再显式报错 |
| `xmlschema/__init__.py` | 导出 `XMLSchemaBaseURLError`、`resolve_relative_url` |
| `tests/test_loading_base.py` | 新增跨工作目录、两形态对照测试（XSD 1.0 与 1.1） |
| `tests/test_resources.py`、`tests/validators/test_schemas.py` | 把依赖旧工作目录漂移的断言更新为显式基准语义；文件源警告链路用独立用例保留 |

## 五、对照测试（同一文本、两种基准、跨工作目录比对读数）

`tests/test_loading_base.py` 中 `test_two_forms_same_text_same_readings_across_working_dirs`：

- 同一份含相对 include 的 schema 文本，分别以
  - **文件路径源**（基准 = 文件所在目录）和
  - **内存文本源 + 显式 `base_url`**
  两种形态装载；
- 在**两个不同工作目录**下各装载一次；
- 断言两种形态解析出的相对件 URL 完全相同，且对同一 XML 实例的 `decode()` 读数完全相同，
  并且这些结果在两个工作目录之间逐字一致。

另覆盖：无基准显式报错且错误指向相对件、`keep_relative` 逃生口、文件源语义保持、显式基准
行为保持、各内存源形态（str/bytes/StringIO/BytesIO/Element）分流、import fallback 不被短路。

## 六、测试结果

- 新增测试模块：14 个用例全部通过（XSD 1.0 / 1.1 两套）。
- 全量既有测试：失败/错误集合与改动前干净基线**完全一致**（基线即存在的 138 failures +
  100 errors 均为本环境预存问题，如 zope.interface 的 pickling `__provides__` 问题、
  codegen 的 jinja 环境问题等），本次改动**零新增回归、零修复既有失败之外的行为变化**。
- `flake8` 干净；改动文件 `mypy` 无新增错误（残余 2 个 mypy 报错位于未改动文件，属预存）。

## 七、升级影响（给调用方）

- 仅影响**从内存文本/字节/流/Element 装载、且内部含相对引用、又未传 `base_url`** 的用法。
  这类调用过去依赖工作目录偶然命中，行为本就不可靠；现在会收到明确的 `XMLSchemaBaseURLError`
  （strict 下包装为 `XMLSchemaParseError`），错误信息给出相对件名与 `base_url` 修复指引。
- 修复方式：装载内存文本时显式传入相对件所在目录，例如
  `XMLSchema(schema_text, base_url='/path/to/spec/dir')`。
- 文件路径源、URL 源、显式 `base_url`、绝对引用、远程引用、`normalize_url()` 直接调用方均
  不受影响。
