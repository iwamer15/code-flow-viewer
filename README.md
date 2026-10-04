# Code Flow Viewer

フォルダを指定すると配下のソースコードをまとめて解析し、
**ツールの起動から処理がどう流れるか**をフローチャート / パイプラインで表示するローカルツールです。
同じ画面にエディタ(Monaco = VS Code と同じエンジン)があり、図を見ながらそのまま編集・保存できます。

- **インストール不要**: Python 3.9 以上の標準ライブラリだけで動きます (pip install なし)
- **Mac / Windows 両対応**: パス・文字コード(UTF-8 / Shift_JIS(cp932) / EUC-JP)・改行コードを保持して保存
- **19 言語に対応**: Python / JavaScript / TypeScript / Java / Kotlin / Scala / C / C++(Arduino .ino 含む) / C# / Go / Rust / Swift / PHP / Dart / R / PowerShell / Ruby / Lua / Shell

## 起動

```bash
# mac
python3 -m codeflow /path/to/project        # フォルダを指定して起動 (ブラウザが自動で開く)
python3 -m codeflow --ask                    # 画面でフォルダを選ぶ (前回のフォルダを復元)
./start.command                              # ダブルクリック起動
```

```bat
rem Windows
start.bat                                    rem ダブルクリック: 画面でフォルダを選ぶ
rem フォルダを start.bat にドラッグ&ドロップ: そのフォルダを読み込む
py -m codeflow C:\work\project
```

オプション: `--port 8765` (使用中なら自動で次のポート) / `--no-browser`

`http://127.0.0.1:8765/` で開きます。サーバは自分の PC (127.0.0.1) からしか接続できず、
読み書きできるのは読み込んだフォルダの中だけです。

### オフライン環境 (社内 PC など)

画面のライブラリ (Cytoscape.js / dagre / Mermaid / Monaco Editor) は通常 CDN から読み込みます。
ネットに繋がらない PC で使う場合は、ネットに繋がる PC で一度だけ次を実行し、フォルダごとコピーしてください。

```bash
python3 -m codeflow.vendor      # codeflow/static/vendor/ に取得 (約 15MB)
```

`vendor/` があればそちらが優先され、CDN には接続しません。

## 画面

| 場所 | 内容 |
|---|---|
| 左: ファイル | ツリー表示。ファイルを開くと関数一覧。絞り込み欄はファイル名・関数名の両方に効く。`＋` で新規ファイル |
| 左: 呼び出しツリー | 起点から呼び出し順に並べたツリー (番号=その関数内で呼ぶ順番、↺=再帰、…=既出) |
| 中央: 起動フロー | 起点(エントリーポイント)から呼び出しを深さ N までたどった図。矢印の数字が呼ぶ順番 |
| 中央: 全体 | 全関数の呼び出し関係。パスで絞り込み可 |
| 中央: モジュール依存 | ファイル間の import 関係 (パイプライン表示) |
| 中央: 関数フローチャート | 選んだ関数の中身を if / for / while / switch / try / return 単位のフローチャートに |
| 右: エディタ | タブ切替・保存(Ctrl/⌘+S)・Ctrl/⌘+クリック(F12)で定義へジャンプ・ホバーで呼び出し元/先の数 |

操作:
- ノードをクリック → 詳細カード(呼び出し先/元・外部呼び出し) + エディタで該当関数を範囲ハイライト
- ノードをダブルクリック → 深さ制限で畳まれた先を展開 (点線枠のノード)
- ノードを右クリック → その関数のフローチャート。フローチャート上の箱をクリックで該当行へ、
  青い二重枠(プロジェクト内関数の呼び出し)をダブルクリックで呼び出し先のフローチャートへ潜る
- エディタのカーソル位置の関数がグラフ上でハイライトされる
- 保存すると自動で再解析して図が更新される。**自動更新** が ON なら VS Code など外部で編集しても 3 秒以内に反映
  (未保存の変更があるタブは上書きしない)
- PNG ボタンで図を画像保存

線の種類: 実線=呼び出し / 紫の点線=コールバック登録 (`Thread(target=f)`, `button.clicked.connect(self.on_click)` など、Python) /
破線=同名候補が複数あり推定 / オレンジ破線=再帰・既に出た関数への呼び出し

## エントリーポイントの推定

起点の候補は次の順で自動推定し、プルダウンで切り替えられます (どの関数からでも「ここを起点に」できます)。

1. `if __name__ == "__main__":` / `package.json` の main・bin・scripts / `pyproject.toml` の scripts (95〜100)
2. `main()` 関数 (C/C++/Go/Rust/Java/Kotlin/C#/Dart など)、Arduino の `setup`/`loop`、`__main__.py`
3. シェル/PowerShell スクリプト本体、`main.py` `app.py` `index.js` `server.js` などのトップレベル処理
4. Flask/FastAPI の `@app.route` などで登録されるハンドラ
5. 呼び出し元のない関数

## 仕組みと精度

| 項目 | 方式 |
|---|---|
| Python | 標準ライブラリ `ast` で正確に解析。`import x as y` / `from . import` の別名、`self.method()`、`x = Klass()` からの型推定、コールバック参照に対応 |
| その他の言語 | コメント・文字列を空白化したテキストに対して、言語ごとの定義パターン + 括弧の対応表で関数範囲を特定し、呼び出しを抽出 (依存ライブラリなし・高速) |
| 呼び出しの解決 | import 別名 → 同じクラス → 同じファイル → import 先ファイル → プロジェクト全体で一意な名前、の順。候補が 2〜3 個なら破線で全部つなぐ |
| 速度 | 変更のあったファイルだけ再解析 (キャッシュ)。大量・大容量のときはマルチプロセスで並列解析 |

実測 (Intel Mac): stable-diffusion-webui 229 ファイル / 2,547 関数 → 初回 0.7〜1.0 秒、2 回目以降 0.1 秒。
1.3MB のバンドル JS も 1.2 秒で解析。大きいグラフ(1,200 ノード)も独自の階層レイアウトで約 1 秒で描画。

**限界 (静的解析なので)**: 動的な呼び出し (`getattr`, `obj[name]()`, DI, イベント名文字列での発火など) や、
受け手の型が分からない `obj.method()` で一般的な名前 (`get`, `run`, `update` など) は「外部・未解決」に分類されます。
C++ のテンプレート/マクロ、Ruby/Lua の括弧なし呼び出しは推定精度が下がります。

## ファイル構成

```
codeflow/
  __main__.py     起動 (CLI)
  server.py       HTTP サーバ (標準ライブラリ / 127.0.0.1 限定 / フォルダ外アクセス禁止)
  project.py      走査・キャッシュ・並列解析・import 解決・コールグラフ・エントリーポイント推定
  pyanalyzer.py   Python 解析 (ast)
  languages.py    言語定義 + 汎用解析 + 関数内フロー抽出 (if/for/switch/try…)
  vendor.py       オフライン用ライブラリ取得
  static/         画面 (index.html / app.js / style.css。ビルド不要)
samples/          動作確認用の多言語サンプル
tests/            python -m unittest discover tests
```

## 今後の拡張候補

- 実行してパイプライン上で「今どこまで進んでいるか」を表示 (web-editor の TODO と共通)
- web-editor (OneDrive 連携エディタ) へのパイプライン可視化機能としての組み込み
- tree-sitter (任意インストール) による他言語の解析精度向上
